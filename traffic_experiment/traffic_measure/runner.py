from __future__ import annotations

import json
import random
import socket
import ssl
import statistics
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .backends import (
    BackendRequest,
    build_backend_request,
    normalized_usage,
    parse_backend_response,
    parse_openai_sse,
)
from .capture import CaptureResult, DumpcapCapture
from .common import append_jsonl, read_jsonl, sha256_file, sha256_json, utc_now
from .http3_client import PersistentHttp3Client, post_http3


@dataclass(frozen=True)
class RunSettings:
    manifest_path: Path
    output_dir: Path
    base_url: str
    model: str
    api_key: str
    sample_limit: int
    repetitions: int
    seed: int
    temperature: float
    max_output_tokens: int
    request_timeout_seconds: float
    observation_seconds: int
    capture_interface: str
    capture_filter: str
    capture_startup_delay_seconds: float
    worker_count: int = 1
    worker_index: int = 0
    no_capture: bool = False
    no_wait_after_request: bool = False
    backend: str = "local_vllm"
    transport: str = "http1"
    connection_mode: str = "warm"
    openrouter_provider: str | None = None
    tls_ca_file: Path | None = None
    curl_executable: str = "curl"
    session_id: str | None = None
    inter_request_delay_seconds: float = 0.0
    request_start_interval_seconds: float = 0.0
    session_budget_seconds: float = 0.0


def parse_sse_lines(lines: Iterable[str]) -> tuple[str, dict[str, Any] | None, str | None]:
    parsed = parse_openai_sse(lines)
    return parsed.text, parsed.usage, parsed.response_id


def _resolved_backend(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise ValueError(f"base URL has no hostname: {base_url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return socket.gethostbyname(parsed.hostname), port


def _trial_rows(
    manifest: list[dict[str, Any]],
    sample_limit: int,
    repetitions: int,
    seed: int,
    worker_count: int = 1,
    worker_index: int = 0,
) -> list[tuple[dict[str, Any], int]]:
    sample_ids = sorted({str(row["sample_id"]) for row in manifest})
    if sample_limit <= 0:
        raise ValueError("sample limit must be positive")
    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    if worker_count <= 0:
        raise ValueError("worker count must be positive")
    if worker_index < 0 or worker_index >= worker_count:
        raise ValueError("worker index must be in [0, worker_count)")
    if len(sample_ids) < sample_limit:
        raise ValueError(f"manifest has {len(sample_ids)} samples, but {sample_limit} were requested")
    chosen_order = random.Random(seed).sample(sample_ids, sample_limit)
    chosen = {
        sample_id
        for position, sample_id in enumerate(chosen_order)
        if position % worker_count == worker_index
    }
    trials = [
        (row, repetition)
        for row in manifest
        if str(row["sample_id"]) in chosen
        for repetition in range(1, repetitions + 1)
    ]
    random.Random(seed + 1 + worker_index).shuffle(trials)
    return trials


def _completed_keys(results_path: Path) -> set[tuple[str, int]]:
    if not results_path.exists():
        return set()
    return {
        (str(row["request_id"]), int(row["repetition"]))
        for row in read_jsonl(results_path)
        if row.get("completed") is True
    }


def _job_id(request_id: str, repetition: int) -> str:
    return sha256_json(
        {
            "request_id": request_id,
            "repetition": repetition,
        }
    )[:24]


def _request_once(
    client: httpx.Client,
    request: BackendRequest,
    backend: str,
) -> dict[str, Any]:
    request_started = utc_now()
    start = time.perf_counter()
    request_json_bytes = len(
        json.dumps(
            request.payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    first_byte_at: str | None = None
    first_token_at: str | None = None
    event_lines: list[str] = []
    content_event_offsets: list[float] = []
    response_sse_bytes = 0
    sse_event_count = 0
    status_code: int | None = None
    response_headers: dict[str, str] = {}

    with client.stream("POST", request.endpoint, headers=request.headers, json=request.payload) as response:
        status_code = response.status_code
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "x-request-id", "server", "date"}
        }
        first_byte_at = utc_now()
        response_headers_offset = time.perf_counter() - start
        response.raise_for_status()
        for line in response.iter_lines():
            line_offset = time.perf_counter() - start
            response_sse_bytes += len(line.encode("utf-8")) + 1
            if line.startswith("data:"):
                sse_event_count += 1
            if line.startswith("data:") and '"content"' in line:
                try:
                    event = json.loads(line[5:].strip())
                    if any(
                        (choice.get("delta") or {}).get("content")
                        for choice in event.get("choices", [])
                    ):
                        content_event_offsets.append(line_offset)
                        if first_token_at is None:
                            first_token_at = utc_now()
                except (json.JSONDecodeError, TypeError):
                    pass
            event_lines.append(line)

    parsed = parse_backend_response(backend, event_lines)
    inter_content_event_seconds = [
        current - previous
        for previous, current in zip(
            content_event_offsets,
            content_event_offsets[1:],
        )
    ]
    return {
        "started_at_utc": request_started,
        "first_byte_at_utc": first_byte_at,
        "first_token_at_utc": first_token_at,
        "finished_at_utc": utc_now(),
        "elapsed_seconds": round(time.perf_counter() - start, 6),
        "time_to_response_headers_seconds": round(response_headers_offset, 6),
        "time_to_first_content_seconds": (
            round(content_event_offsets[0], 6) if content_event_offsets else None
        ),
        "content_stream_seconds": (
            round(content_event_offsets[-1] - content_event_offsets[0], 6)
            if len(content_event_offsets) > 1
            else 0.0 if content_event_offsets else None
        ),
        "inter_content_event_p50_seconds": (
            round(statistics.median(inter_content_event_seconds), 6)
            if inter_content_event_seconds
            else None
        ),
        "inter_content_event_p95_seconds": (
            round(
                sorted(inter_content_event_seconds)[
                    max(0, int(0.95 * len(inter_content_event_seconds)) - 1)
                ],
                6,
            )
            if inter_content_event_seconds
            else None
        ),
        "request_json_bytes": request_json_bytes,
        "response_sse_bytes": response_sse_bytes,
        "sse_event_count": sse_event_count,
        "content_event_count": len(content_event_offsets),
        "http_status": status_code,
        "response_headers": response_headers,
        "provider_response_id": parsed.response_id,
        "provider": parsed.provider,
        "model_version": parsed.model_version,
        "finish_reason": parsed.finish_reason,
        "response_text": parsed.text,
        "usage": parsed.usage,
        "negotiated_http_version": response.http_version,
    }


def _request_once_curl(
    request: BackendRequest,
    backend: str,
    transport: str,
    timeout_seconds: float,
    curl_executable: str,
    tls_ca_file: Path | None,
) -> dict[str, Any]:
    request_started = utc_now()
    start = time.perf_counter()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(request.payload, handle, ensure_ascii=False)
        payload_path = Path(handle.name)
    try:
        command = [
            curl_executable,
            "--silent",
            "--show-error",
            "--no-buffer",
            "--max-time",
            str(timeout_seconds),
            "--request",
            "POST",
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            f"@{payload_path}",
        ]
        for name, value in request.headers.items():
            command.extend(["--header", f"{name}: {value}"])
        if transport == "tls13":
            command.extend(["--http1.1", "--tlsv1.3", "--tls-max", "1.3"])
        else:
            raise ValueError("curl transport must be tls13")
        if tls_ca_file:
            command.extend(["--cacert", str(tls_ca_file)])
        command.extend(
            [
                "--write-out",
                "\n__TRAFFIC_META__%{http_code},%{http_version},%{time_starttransfer}\n",
                request.endpoint,
            ]
        )
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        if process.returncode:
            raise RuntimeError(f"curl exited {process.returncode}: {process.stderr.strip()}")
        marker = "\n__TRAFFIC_META__"
        body, metadata = process.stdout.rsplit(marker, 1)
        status, http_version, time_starttransfer = metadata.strip().split(",", 2)
        if int(status) >= 400:
            raise RuntimeError(f"HTTP {status}: {body[-1000:]}")
        parsed = parse_backend_response(backend, body.splitlines())
        return {
            "started_at_utc": request_started,
            "first_byte_at_utc": None,
            "first_token_at_utc": None,
            "finished_at_utc": utc_now(),
            "elapsed_seconds": round(time.perf_counter() - start, 6),
            "time_to_first_byte_seconds": float(time_starttransfer),
            "http_status": int(status),
            "response_headers": {},
            "provider_response_id": parsed.response_id,
            "provider": parsed.provider,
            "model_version": parsed.model_version,
            "finish_reason": parsed.finish_reason,
            "response_text": parsed.text,
            "usage": parsed.usage,
            "negotiated_http_version": http_version,
        }
    finally:
        payload_path.unlink(missing_ok=True)


def _request_once_http3(
    request: BackendRequest,
    backend: str,
    timeout_seconds: float,
    tls_ca_file: Path | None,
    client: PersistentHttp3Client | None = None,
) -> dict[str, Any]:
    request_started = utc_now()
    start = time.perf_counter()
    if client is None:
        response = post_http3(
            request.endpoint,
            request.headers,
            request.payload,
            timeout_seconds,
            tls_ca_file,
        )
    else:
        response = client.post(
            request.endpoint,
            request.headers,
            request.payload,
            timeout_seconds,
        )
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}: {response.body[-1000:]}")
    parsed = parse_backend_response(backend, response.body.splitlines())
    return {
        "started_at_utc": request_started,
        "first_byte_at_utc": None,
        "first_token_at_utc": None,
        "finished_at_utc": utc_now(),
        "elapsed_seconds": round(time.perf_counter() - start, 6),
        "time_to_first_byte_seconds": response.time_to_first_byte_seconds,
        "http_status": response.status,
        "response_headers": response.headers,
        "provider_response_id": parsed.response_id,
        "provider": parsed.provider,
        "model_version": parsed.model_version,
        "finish_reason": parsed.finish_reason,
        "response_text": parsed.text,
        "usage": parsed.usage,
        "negotiated_http_version": "3",
    }


def health_check(base_url: str, api_key: str, timeout_seconds: float = 10) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(headers=headers, timeout=timeout_seconds) as client:
        response = client.get(base_url.rstrip("/") + "/models")
        response.raise_for_status()
        return response.json()


def _warm_http3_client(
    client: PersistentHttp3Client | None,
    base_url: str,
    api_key: str,
    ca_file: Path | None,
    timeout_seconds: float = 5,
) -> PersistentHttp3Client:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    models_url = base_url.rstrip("/") + "/models"
    if client is not None:
        try:
            response = client.get(models_url, headers, timeout_seconds)
            if response.status < 400:
                return client
        except Exception:
            client.close()
    replacement = PersistentHttp3Client(base_url, ca_file)
    response = replacement.get(models_url, headers, timeout_seconds)
    if response.status >= 400:
        replacement.close()
        raise RuntimeError(f"HTTP/3 warm-up failed with HTTP {response.status}")
    return replacement


def run_experiment(settings: RunSettings) -> Path:
    manifest = read_jsonl(settings.manifest_path)
    trials = _trial_rows(
        manifest,
        settings.sample_limit,
        settings.repetitions,
        settings.seed,
        worker_count=settings.worker_count,
        worker_index=settings.worker_index,
    )
    results_path = settings.output_dir / "results.jsonl"
    captures_dir = settings.output_dir / "captures"
    completed = _completed_keys(results_path)
    backend_ip, backend_port = _resolved_backend(settings.base_url)
    if settings.transport not in {"http1", "tls13", "http3"}:
        raise ValueError("transport must be http1, tls13, or http3")
    if settings.connection_mode not in {"warm", "cold"}:
        raise ValueError("connection mode must be warm or cold")
    if settings.inter_request_delay_seconds < 0:
        raise ValueError("inter-request delay must be non-negative")
    if settings.request_start_interval_seconds < 0:
        raise ValueError("request-start interval must be non-negative")
    if settings.session_budget_seconds < 0:
        raise ValueError("session budget must be non-negative")
    if (
        settings.inter_request_delay_seconds > 0
        and settings.request_start_interval_seconds > 0
    ):
        raise ValueError(
            "use either inter-request delay or request-start interval, not both"
        )

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    verify: ssl.SSLContext | str | bool
    if settings.transport == "tls13":
        verify = ssl.create_default_context(
            cafile=str(settings.tls_ca_file) if settings.tls_ca_file else None
        )
        verify.minimum_version = ssl.TLSVersion.TLSv1_3
        verify.maximum_version = ssl.TLSVersion.TLSv1_3
    else:
        verify = str(settings.tls_ca_file) if settings.tls_ca_file else True
    shared_client = httpx.Client(
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        verify=verify,
        http1=True,
        http2=False,
    )
    shared_http3: PersistentHttp3Client | None = None
    session_started_monotonic = time.perf_counter()
    try:
        if settings.transport == "tls13" and settings.connection_mode == "warm":
            warm_headers = (
                {"Authorization": f"Bearer {settings.api_key}"}
                if settings.api_key
                else {}
            )
            warm_response = shared_client.get(
                settings.base_url.rstrip("/") + "/models",
                headers=warm_headers,
            )
            warm_response.raise_for_status()
        elif settings.transport == "http3" and settings.connection_mode == "warm":
            shared_http3 = PersistentHttp3Client(
                settings.base_url,
                settings.tls_ca_file,
            )

        for index, (request, repetition) in enumerate(trials, start=1):
            session_elapsed_at_start = (
                time.perf_counter() - session_started_monotonic
            )
            if (
                index > 1
                and settings.session_budget_seconds > 0
                and session_elapsed_at_start >= settings.session_budget_seconds
            ):
                print(
                    "stop session before next request: "
                    f"{session_elapsed_at_start:.3f}s elapsed "
                    f"(budget={settings.session_budget_seconds:.3f}s)",
                    flush=True,
                )
                break
            trial_started_monotonic = time.perf_counter()
            key = (str(request["request_id"]), repetition)
            if key in completed:
                print(f"[{index}/{len(trials)}] skip completed {key[0]} repetition={repetition}", flush=True)
                continue

            run_uuid = uuid.uuid4().hex
            job_id = _job_id(key[0], repetition)
            capture_path = captures_dir / f"{job_id}.pcapng"
            partial_capture_path = captures_dir / f"{job_id}.partial.pcapng"
            partial_capture_path.unlink(missing_ok=True)
            generation = {
                "temperature": settings.temperature,
                "max_tokens": settings.max_output_tokens,
                "stream": True,
                "seed": settings.seed,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
            }
            backend_request = build_backend_request(
                backend=settings.backend,
                base_url=settings.base_url,
                model=settings.model,
                api_key=settings.api_key,
                messages=request["messages"],
                generation=generation,
                openrouter_provider=settings.openrouter_provider,
            )
            request_sha = sha256_json(backend_request.payload)
            capture: DumpcapCapture | None = None
            capture_result = CaptureResult(path=None, return_code=None, stderr="")
            response_data: dict[str, Any] = {}
            error: str | None = None

            print(f"[{index}/{len(trials)}] run {key[0]} repetition={repetition}", flush=True)
            try:
                if settings.transport == "http3" and settings.connection_mode == "warm":
                    shared_http3 = _warm_http3_client(
                        shared_http3,
                        settings.base_url,
                        settings.api_key,
                        settings.tls_ca_file,
                    )
                if not settings.no_capture:
                    capture = DumpcapCapture(
                        output_path=partial_capture_path,
                        interface=settings.capture_interface,
                        capture_filter=settings.capture_filter,
                        duration_seconds=settings.observation_seconds,
                        startup_delay_seconds=settings.capture_startup_delay_seconds,
                    )
                    capture.start()
                if settings.transport == "http1":
                    client = (
                        shared_client
                        if settings.connection_mode == "warm"
                        else httpx.Client(
                            timeout=httpx.Timeout(settings.request_timeout_seconds),
                            verify=str(settings.tls_ca_file) if settings.tls_ca_file else True,
                        )
                    )
                    try:
                        response_data = _request_once(client, backend_request, settings.backend)
                    finally:
                        if settings.connection_mode == "cold":
                            client.close()
                elif settings.transport == "tls13":
                    if settings.connection_mode == "warm":
                        response_data = _request_once(
                            shared_client,
                            backend_request,
                            settings.backend,
                        )
                    else:
                        response_data = _request_once_curl(
                            backend_request,
                            settings.backend,
                            settings.transport,
                            settings.request_timeout_seconds,
                            settings.curl_executable,
                            settings.tls_ca_file,
                        )
                else:
                    response_data = _request_once_http3(
                        backend_request,
                        settings.backend,
                        settings.request_timeout_seconds,
                        settings.tls_ca_file,
                        client=shared_http3,
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if capture is not None:
                    capture_result = capture.finish()
                elif not settings.no_wait_after_request and settings.observation_seconds > 0:
                    # Dry-run tests can disable this wait. Real no-capture runs preserve pacing.
                    time.sleep(settings.observation_seconds)

            capture_sha = (
                None
            )
            completed_ok = error is None and bool(response_data.get("response_text"))
            if (
                completed_ok
                and capture_result.path is not None
                and capture_result.path.exists()
            ):
                capture_result.path.replace(capture_path)
                capture_result.path = capture_path
                capture_sha = sha256_file(capture_path)
            result = {
                "run_id": run_uuid,
                "job_id": job_id,
                "request_id": request["request_id"],
                "sample_id": request["sample_id"],
                "conversation_id": request["conversation_id"],
                "question_id": request.get("question_id"),
                "task_type": request.get("task_type", "qa"),
                "reference_answer": request.get("reference_answer"),
                "target_speaker": request.get("target_speaker"),
                "condition": request["condition"],
                "repetition": repetition,
                "worker_count": settings.worker_count,
                "worker_index": settings.worker_index,
                "backend": settings.backend,
                "backend_ip": backend_ip,
                "backend_port": backend_port,
                "model": settings.model,
                "transport": settings.transport,
                "connection_mode": settings.connection_mode,
                "session_id": settings.session_id,
                "inter_request_delay_seconds": settings.inter_request_delay_seconds,
                "request_start_interval_seconds": (
                    settings.request_start_interval_seconds
                ),
                "session_budget_seconds": settings.session_budget_seconds,
                "session_elapsed_at_request_start_seconds": round(
                    session_elapsed_at_start,
                    6,
                ),
                "request_sha256": request_sha,
                "messages_sha256": request["messages_sha256"],
                "capture_file": str(capture_result.path) if capture_result.path else None,
                "capture_sha256": capture_sha,
                "capture_interface": settings.capture_interface if not settings.no_capture else None,
                "capture_filter": settings.capture_filter if not settings.no_capture else None,
                "capture_return_code": capture_result.return_code,
                "capture_stderr": capture_result.stderr or None,
                "capture_observation_seconds": settings.observation_seconds,
                "capture_may_be_truncated": (
                    response_data.get("elapsed_seconds", 0) > settings.observation_seconds
                ),
                "generation": generation,
                "completed": completed_ok,
                "error": error,
                **response_data,
            }
            result["input_tokens"], result["output_tokens"] = normalized_usage(
                settings.backend, result.get("usage")
            )
            first_content_seconds = result.get("time_to_first_content_seconds")
            elapsed_seconds = result.get("elapsed_seconds")
            output_tokens = result.get("output_tokens")
            if (
                isinstance(output_tokens, int)
                and output_tokens > 0
                and isinstance(first_content_seconds, (int, float))
                and isinstance(elapsed_seconds, (int, float))
                and elapsed_seconds > first_content_seconds
            ):
                result["post_first_content_tokens_per_second"] = round(
                    output_tokens / (elapsed_seconds - first_content_seconds),
                    6,
                )
            else:
                result["post_first_content_tokens_per_second"] = None
            result["session_elapsed_at_completion_seconds"] = round(
                time.perf_counter() - session_started_monotonic,
                6,
            )
            result["session_budget_exceeded_on_completion"] = (
                settings.session_budget_seconds > 0
                and result["session_elapsed_at_completion_seconds"]
                >= settings.session_budget_seconds
            )
            append_jsonl(results_path, result)
            if completed_ok:
                completed.add(key)
            if (
                index < len(trials)
                and settings.request_start_interval_seconds > 0
            ):
                elapsed_since_start = time.perf_counter() - trial_started_monotonic
                time.sleep(
                    max(
                        0.0,
                        settings.request_start_interval_seconds
                        - elapsed_since_start,
                    )
                )
            elif (
                index < len(trials)
                and settings.inter_request_delay_seconds > 0
            ):
                time.sleep(settings.inter_request_delay_seconds)

    finally:
        if shared_http3 is not None:
            shared_http3.close()
        shared_client.close()
    return results_path
