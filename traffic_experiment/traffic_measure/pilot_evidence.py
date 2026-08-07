from __future__ import annotations

import argparse
import json
import os
import random
import re
import stat
import sys
from pathlib import Path
from typing import Any

from .backends import build_backend_request
from .common import read_jsonl, sha256_file, sha256_json, utc_now


SCHEMA = "commu-protocol-pilot-evidence-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalized_http_version(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"1.1", "HTTP/1.1"}:
        return "1.1"
    if normalized in {"3", "3.0", "HTTP/3", "HTTP/3.0"}:
        return "3"
    return normalized


def _capture_path(results_path: Path, value: Any) -> Path:
    if not value:
        raise ValueError("result row has no capture_file")
    capture = Path(str(value))
    if not capture.is_absolute():
        capture = results_path.parent / capture
    if capture.is_symlink():
        raise ValueError(f"capture must not be a symlink: {capture}")
    return capture.resolve(strict=True)


def validate_result_evidence(
    *,
    results_path: Path,
    manifest_path: Path,
    transport: str,
    model: str,
    manifest_sha256: str,
    model_revision: str,
    protocol_stack_sha256: str,
    network_mtu: int,
    backend_ip: str,
    capture_interface: str,
    capture_filter: str,
    seed: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    if results_path.is_symlink():
        raise ValueError(f"results evidence must not be a symlink: {results_path}")
    results_path = results_path.resolve(strict=True)
    if not results_path.is_file():
        raise ValueError(f"results evidence is not a regular file: {results_path}")
    for name, digest in (
        ("manifest_sha256", manifest_sha256),
        ("protocol_stack_sha256", protocol_stack_sha256),
    ):
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"{name} is malformed")
    if manifest_path.is_symlink():
        raise ValueError(f"manifest must not be a symlink: {manifest_path}")
    manifest_path = manifest_path.resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError(f"manifest is not a regular file: {manifest_path}")
    if sha256_file(manifest_path) != manifest_sha256:
        raise ValueError("manifest SHA-256 does not match the expected digest")
    rows = read_jsonl(results_path)
    if len(rows) != 1:
        raise ValueError(
            f"expected exactly one protocol-pilot row, found {len(rows)}"
        )
    row = rows[0]
    expected_http = "3" if transport == "http3" else "1.1"
    checks = {
        "completed": row.get("completed") is True,
        "error": not row.get("error"),
        "condition": row.get("condition") == "no_compression",
        "transport": row.get("transport") == transport,
        "backend": row.get("backend") == "local_vllm",
        "model": row.get("model") == model,
        "model_version": row.get("model_version") == model,
        "manifest_sha256": row.get("manifest_sha256") == manifest_sha256,
        "backend_ip": row.get("backend_ip") == backend_ip,
        "backend_port": row.get("backend_port")
        == (8444 if transport == "http3" else 8443),
        "connection_mode": row.get("connection_mode") == "warm",
        "finish_reason": row.get("finish_reason") == "stop",
        "negotiated_http_version": (
            _normalized_http_version(row.get("negotiated_http_version"))
            == expected_http
        ),
        "capture_interface": row.get("capture_interface") == capture_interface,
        "capture_filter": row.get("capture_filter") == capture_filter,
        "capture_return_code": row.get("capture_return_code") == 0,
        "capture_stop_on_response": row.get("capture_stop_on_response") is True,
        "capture_may_be_truncated": row.get("capture_may_be_truncated") is False,
        "capture_observation_seconds": row.get("capture_observation_seconds") == 900,
        "repetition": row.get("repetition") == 1,
        "worker_count": row.get("worker_count") == 1,
        "worker_index": row.get("worker_index") == 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(
            "protocol-pilot result mismatch: " + ", ".join(failed)
        )

    generation = row.get("generation") or {}
    expected_generation = {
        "seed": seed,
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": True,
    }
    generation_failed = [
        name
        for name, expected in expected_generation.items()
        if generation.get(name) != expected
    ]
    if generation_failed:
        raise ValueError(
            "protocol-pilot generation mismatch: "
            + ", ".join(generation_failed)
        )

    manifest = [
        candidate
        for candidate in read_jsonl(manifest_path)
        if candidate.get("condition") == "no_compression"
    ]
    sample_ids = sorted({str(candidate.get("sample_id")) for candidate in manifest})
    if not sample_ids:
        raise ValueError("manifest has no no_compression samples")
    selected_sample_id = random.Random(seed).sample(sample_ids, 1)[0]
    selected_rows = [
        candidate
        for candidate in manifest
        if str(candidate.get("sample_id")) == selected_sample_id
    ]
    if len(selected_rows) != 1:
        raise ValueError(
            "selected protocol-pilot sample does not map to exactly one "
            "no_compression manifest row"
        )
    selected = selected_rows[0]
    request_id = str(row.get("request_id") or "")
    if not request_id:
        raise ValueError("protocol-pilot result has no request_id")
    for key in (
        "request_id",
        "sample_id",
        "conversation_id",
        "question_id",
        "task_type",
        "messages_sha256",
    ):
        if row.get(key) != selected.get(key):
            raise ValueError(f"result does not match selected manifest row for {key}")
    backend_request = build_backend_request(
        backend="local_vllm",
        base_url="https://protocol-pilot.invalid/v1",
        model=model,
        api_key="not-used-for-payload",
        messages=selected["messages"],
        generation={
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "stream": True,
            "seed": seed,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        },
        openrouter_provider=None,
    )
    expected_request_sha256 = sha256_json(backend_request.payload)
    if row.get("request_sha256") != expected_request_sha256:
        raise ValueError("result request_sha256 does not match the selected request")
    capture = _capture_path(results_path, row.get("capture_file"))
    if not capture.is_file() or capture.stat().st_size <= 0:
        raise ValueError(f"capture is missing, empty, or a symlink: {capture}")
    capture_sha256 = str(row.get("capture_sha256") or "").lower()
    if not _SHA256_RE.fullmatch(capture_sha256):
        raise ValueError("result capture_sha256 is missing or malformed")
    actual_capture_sha256 = sha256_file(capture)
    if actual_capture_sha256 != capture_sha256:
        raise ValueError(
            "capture SHA-256 does not match the protocol-pilot result row"
        )

    return {
        "schema": SCHEMA,
        "status": "success",
        "results_file": str(results_path),
        "results_sha256": sha256_file(results_path),
        "request_id": request_id,
        "request_sha256": expected_request_sha256,
        "capture_file": str(capture),
        "capture_sha256": capture_sha256,
        "transport": transport,
        "negotiated_http_version": expected_http,
        "model": model,
        "manifest_sha256": manifest_sha256,
        "manifest_file": str(manifest_path),
        "model_revision": model_revision,
        "protocol_stack_sha256": protocol_stack_sha256,
        "network_mtu": network_mtu,
        "backend_ip": backend_ip,
        "backend_port": 8444 if transport == "http3" else 8443,
        "capture_interface": capture_interface,
        "capture_filter": capture_filter,
        "condition": "no_compression",
        "connection_mode": "warm",
        "seed": seed,
        "temperature": 0,
        "max_output_tokens": max_output_tokens,
    }


def write_evidence_marker(marker_path: Path, evidence: dict[str, Any]) -> None:
    if marker_path.is_symlink():
        raise FileExistsError(
            f"refusing to overwrite protocol-pilot evidence marker: {marker_path}"
        )
    marker_path = marker_path.resolve()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    if marker_path.exists() or marker_path.is_symlink():
        raise FileExistsError(
            f"refusing to overwrite protocol-pilot evidence marker: {marker_path}"
        )
    payload = dict(evidence)
    payload["pcap_protocol_validated"] = True
    payload["validated_at_utc"] = utc_now()
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            marker_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o444,
        )
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(marker_path, 0o444)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            marker_path.unlink(missing_ok=True)
        raise


def verify_evidence_marker(
    *,
    marker_path: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    if marker_path.is_symlink():
        raise ValueError(f"evidence marker must not be a symlink: {marker_path}")
    marker_path = marker_path.resolve(strict=True)
    if not marker_path.is_file():
        raise ValueError(f"evidence marker is not a regular file: {marker_path}")
    if os.name != "nt":
        marker_stat = marker_path.stat()
        if marker_stat.st_uid != os.geteuid():
            raise ValueError("evidence marker is not owned by the current user")
        if stat.S_IMODE(marker_stat.st_mode) & 0o222:
            raise ValueError("evidence marker must not be writable")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("pcap_protocol_validated") is not True:
        raise ValueError("evidence marker lacks PCAP protocol validation")
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError(
                f"evidence marker mismatch for {key}: "
                f"expected {value!r}, got {marker.get(key)!r}"
            )
    return marker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate immutable protocol-pilot result/PCAP evidence"
    )
    parser.add_argument("action", choices=["check", "mark", "verify"])
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--transport", choices=["tls13", "http3"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--protocol-stack-sha256", required=True)
    parser.add_argument("--network-mtu", type=int, required=True)
    parser.add_argument("--backend-ip", required=True)
    parser.add_argument("--capture-interface", required=True)
    parser.add_argument("--capture-filter", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--max-output-tokens", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        results_path = args.results
        if args.action == "verify" and results_path is None:
            marker_preview = json.loads(
                args.marker.read_text(encoding="utf-8")
            )
            results_value = marker_preview.get("results_file")
            if not results_value:
                raise ValueError("evidence marker has no results_file")
            results_path = Path(str(results_value))
        if results_path is None:
            raise ValueError(f"--results is required for {args.action}")
        evidence = validate_result_evidence(
            results_path=results_path,
            manifest_path=args.manifest,
            transport=args.transport,
            model=args.model,
            manifest_sha256=args.manifest_sha256,
            model_revision=args.model_revision,
            protocol_stack_sha256=args.protocol_stack_sha256,
            network_mtu=args.network_mtu,
            backend_ip=args.backend_ip,
            capture_interface=args.capture_interface,
            capture_filter=args.capture_filter,
            seed=args.seed,
            max_output_tokens=args.max_output_tokens,
        )
        if args.action == "mark":
            write_evidence_marker(args.marker, evidence)
        elif args.action == "verify":
            verify_evidence_marker(marker_path=args.marker, expected=evidence)
        print(evidence["capture_file"])
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"protocol-pilot evidence rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
