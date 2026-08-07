from __future__ import annotations

import json
import os
import random
import stat
from pathlib import Path

import pytest

from traffic_experiment.traffic_measure.backends import build_backend_request
from traffic_experiment.traffic_measure.common import sha256_file, sha256_json
from traffic_experiment.traffic_measure.pilot_evidence import (
    validate_result_evidence,
    verify_evidence_marker,
    write_evidence_marker,
)


MODEL = "Qwen/Qwen3-32B"
REVISION = "frozen-revision"
STACK_SHA256 = "b" * 64
SEED = 42
MAX_OUTPUT_TOKENS = 4096


def _write_fixture(tmp_path: Path, transport: str = "tls13") -> tuple[Path, Path]:
    manifest_path = tmp_path / "requests.jsonl"
    manifest: list[dict[str, object]] = []
    for index in range(3):
        messages = [
            {"role": "system", "content": "answer from context"},
            {"role": "user", "content": f"question {index}"},
        ]
        manifest.append(
            {
                "request_id": f"sample-{index}::no_compression",
                "sample_id": f"sample-{index}",
                "conversation_id": f"conversation-{index}",
                "question_id": f"question-{index}",
                "task_type": "qa",
                "condition": "no_compression",
                "messages": messages,
                "messages_sha256": sha256_json(messages),
            }
        )
    manifest_path.write_text(
        "".join(json.dumps(row) + "\n" for row in manifest),
        encoding="utf-8",
    )

    selected_sample = random.Random(SEED).sample(
        sorted(str(row["sample_id"]) for row in manifest),
        1,
    )[0]
    selected = next(
        row for row in manifest if row["sample_id"] == selected_sample
    )
    generation = {
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": True,
        "seed": SEED,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = build_backend_request(
        backend="local_vllm",
        base_url="https://protocol-pilot.invalid/v1",
        model=MODEL,
        api_key="unused",
        messages=selected["messages"],  # type: ignore[arg-type]
        generation=generation,
    )
    capture_path = tmp_path / "capture.pcapng"
    capture_path.write_bytes(b"pcap evidence")
    row = {
        "request_id": selected["request_id"],
        "sample_id": selected["sample_id"],
        "conversation_id": selected["conversation_id"],
        "question_id": selected["question_id"],
        "task_type": selected["task_type"],
        "condition": "no_compression",
        "repetition": 1,
        "worker_count": 1,
        "worker_index": 0,
        "backend": "local_vllm",
        "backend_ip": "10.200.0.1",
        "backend_port": 8444 if transport == "http3" else 8443,
        "model": MODEL,
        "model_version": MODEL,
        "transport": transport,
        "connection_mode": "warm",
        "negotiated_http_version": "HTTP/3"
        if transport == "http3"
        else "HTTP/1.1",
        "finish_reason": "stop",
        "messages_sha256": selected["messages_sha256"],
        "request_sha256": sha256_json(request.payload),
        "manifest_sha256": sha256_file(manifest_path),
        "capture_file": str(capture_path),
        "capture_sha256": sha256_file(capture_path),
        "capture_interface": "llmclient0",
        "capture_filter": "(udp port 8444 or tcp port 8444)"
        if transport == "http3"
        else "tcp port 8443",
        "capture_return_code": 0,
        "capture_observation_seconds": 900,
        "capture_stop_on_response": True,
        "capture_may_be_truncated": False,
        "generation": generation,
        "completed": True,
        "error": None,
    }
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return manifest_path, results_path


def _validate(manifest_path: Path, results_path: Path, transport: str = "tls13"):
    return validate_result_evidence(
        results_path=results_path,
        manifest_path=manifest_path,
        transport=transport,
        model=MODEL,
        manifest_sha256=sha256_file(manifest_path),
        model_revision=REVISION,
        protocol_stack_sha256=STACK_SHA256,
        network_mtu=1500,
        backend_ip="10.200.0.1",
        capture_interface="llmclient0",
        capture_filter="(udp port 8444 or tcp port 8444)"
        if transport == "http3"
        else "tcp port 8443",
        seed=SEED,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )


@pytest.mark.parametrize("transport", ["tls13", "http3"])
def test_pilot_marker_binds_selected_request_result_and_capture(
    tmp_path: Path,
    transport: str,
):
    manifest_path, results_path = _write_fixture(tmp_path, transport)
    evidence = _validate(manifest_path, results_path, transport)
    marker = tmp_path / "PILOT_EVIDENCE_OK.json"

    write_evidence_marker(marker, evidence)
    verified = verify_evidence_marker(marker_path=marker, expected=evidence)

    assert verified["pcap_protocol_validated"] is True
    assert verified["request_sha256"] == evidence["request_sha256"]
    assert verified["results_sha256"] == sha256_file(results_path)
    if os.name != "nt":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o444


def test_pilot_evidence_rejects_changed_pcap_and_wrong_result_identity(
    tmp_path: Path,
):
    manifest_path, results_path = _write_fixture(tmp_path)
    evidence = _validate(manifest_path, results_path)
    marker = tmp_path / "PILOT_EVIDENCE_OK.json"
    write_evidence_marker(marker, evidence)

    capture = Path(str(evidence["capture_file"]))
    capture.write_bytes(b"changed capture")
    with pytest.raises(ValueError, match="capture SHA-256"):
        _validate(manifest_path, results_path)

    capture.write_bytes(b"pcap evidence")
    row = json.loads(results_path.read_text(encoding="utf-8"))
    row["model"] = "wrong-model"
    results_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="protocol-pilot result mismatch"):
        _validate(manifest_path, results_path)


def test_pilot_evidence_rejects_symlinked_results(tmp_path: Path):
    manifest_path, results_path = _write_fixture(tmp_path)
    link = tmp_path / "results-link.jsonl"
    try:
        link.symlink_to(results_path)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="must not be a symlink"):
        _validate(manifest_path, link)
