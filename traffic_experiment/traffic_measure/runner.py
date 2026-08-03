from __future__ import annotations

import json
import random
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from .capture import CaptureResult, DumpcapCapture
from .common import append_jsonl, read_jsonl, sha256_file, sha256_json, utc_now


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


def parse_sse_lines(lines: Iterable[str]) -> tuple[str, dict[str, Any] | None, str | None]:
    pieces: list[str] = []
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        response_id = response_id or event.get("id")
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                pieces.append(str(content))
    return "".join(pieces), usage, response_id


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


def _request_once(
    client: httpx.Client,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_started = utc_now()
    start = time.perf_counter()
    first_byte_at: str | None = None
    first_token_at: str | None = None
    event_lines: list[str] = []
    status_code: int | None = None
    response_headers: dict[str, str] = {}

    with client.stream("POST", endpoint, json=payload) as response:
        status_code = response.status_code
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "x-request-id", "server", "date"}
        }
        first_byte_at = utc_now()
        response.raise_for_status()
        for line in response.iter_lines():
            if first_token_at is None and line.startswith("data:") and '"content"' in line:
                try:
                    event = json.loads(line[5:].strip())
                    if any((choice.get("delta") or {}).get("content") for choice in event.get("choices", [])):
                        first_token_at = utc_now()
                except (json.JSONDecodeError, TypeError):
                    pass
            event_lines.append(line)

    text, usage, response_id = parse_sse_lines(event_lines)
    return {
        "started_at_utc": request_started,
        "first_byte_at_utc": first_byte_at,
        "first_token_at_utc": first_token_at,
        "finished_at_utc": utc_now(),
        "elapsed_seconds": round(time.perf_counter() - start, 6),
        "http_status": status_code,
        "response_headers": response_headers,
        "provider_response_id": response_id,
        "response_text": text,
        "usage": usage,
    }


def health_check(base_url: str, api_key: str, timeout_seconds: float = 10) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(headers=headers, timeout=timeout_seconds) as client:
        response = client.get(base_url.rstrip("/") + "/models")
        response.raise_for_status()
        return response.json()


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
    endpoint = settings.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout(settings.request_timeout_seconds),
    ) as client:
        for index, (request, repetition) in enumerate(trials, start=1):
            key = (str(request["request_id"]), repetition)
            if key in completed:
                print(f"[{index}/{len(trials)}] skip completed {key[0]} repetition={repetition}", flush=True)
                continue

            run_uuid = uuid.uuid4().hex
            safe_request_id = sha256_json(request["request_id"])[:16]
            capture_path = captures_dir / f"{safe_request_id}_r{repetition}_{run_uuid[:8]}.pcapng"
            generation = {
                "temperature": settings.temperature,
                "max_tokens": settings.max_output_tokens,
                "stream": True,
                "seed": settings.seed,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
            }
            payload = {"model": settings.model, "messages": request["messages"], **generation}
            request_sha = sha256_json(payload)
            capture: DumpcapCapture | None = None
            capture_result = CaptureResult(path=None, return_code=None, stderr="")
            response_data: dict[str, Any] = {}
            error: str | None = None

            print(f"[{index}/{len(trials)}] run {key[0]} repetition={repetition}", flush=True)
            try:
                if not settings.no_capture:
                    capture = DumpcapCapture(
                        output_path=capture_path,
                        interface=settings.capture_interface,
                        capture_filter=settings.capture_filter,
                        duration_seconds=settings.observation_seconds,
                        startup_delay_seconds=settings.capture_startup_delay_seconds,
                    )
                    capture.start()
                response_data = _request_once(client, endpoint, payload)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if capture is not None:
                    capture_result = capture.finish()
                elif not settings.no_wait_after_request and settings.observation_seconds > 0:
                    # Dry-run tests can disable this wait. Real no-capture runs preserve pacing.
                    time.sleep(settings.observation_seconds)

            capture_sha = (
                sha256_file(capture_result.path)
                if capture_result.path is not None and capture_result.path.exists()
                else None
            )
            completed_ok = error is None and bool(response_data.get("response_text"))
            result = {
                "run_id": run_uuid,
                "request_id": request["request_id"],
                "sample_id": request["sample_id"],
                "conversation_id": request["conversation_id"],
                "question_id": request["question_id"],
                "condition": request["condition"],
                "repetition": repetition,
                "worker_count": settings.worker_count,
                "worker_index": settings.worker_index,
                "backend": "local_vllm",
                "backend_ip": backend_ip,
                "backend_port": backend_port,
                "model": settings.model,
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
            usage = result.get("usage") or {}
            result["input_tokens"] = usage.get("prompt_tokens")
            result["output_tokens"] = usage.get("completion_tokens")
            append_jsonl(results_path, result)
            if completed_ok:
                completed.add(key)

    return results_path
