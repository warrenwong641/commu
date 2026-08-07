"""Deterministic admission-rule tests using a fake/injected clock and
mocked backend.  These verify the runner-level behaviour, not just the
post-hoc timeline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from traffic_measure.runner import RunSettings, run_experiment


# ---- helpers -----------------------------------------------------------

def _fake_chat_response(content: str) -> bytes:
    """Return a minimal OpenAI-compatible SSE stream."""
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1750000000,
        "model": "test-model",
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ],
    }
    lines = ["data: " + json.dumps(payload)]
    payload["choices"][0]["finish_reason"] = "stop"
    payload["choices"][0]["delta"] = {}
    lines.append("data: " + json.dumps(payload))
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n\n").encode()


def _fake_manifest(n: int = 5) -> list[dict]:
    return [
        {
            "request_id": f"req-{i}",
            "sample_id": f"sample-{i}",
            "conversation_id": f"conv-{i}",
            "condition": "no_compression",
            "messages": [{"role": "user", "content": f"question {i}"}],
            "messages_sha256": f"sha256-{i:032x}",
        }
        for i in range(n)
    ]


# ---- tests -------------------------------------------------------------

def _run_with_clock(
    manifest_rows: list[dict],
    admission_elapsed: list[float],
    *,
    tmp_path: Path,
    session_budget_seconds: float = 30.0,
    transport: str = "http1",
    connection_mode: str = "cold",
) -> int:
    """Run the experiment with a semantic fake clock.

    *admission_elapsed* lists the session-elapsed seconds at which
    each successive trial *completes*.  Internally, perf_counter
    returns values that produce the desired elapsed deltas regardless
    of how many intermediate calls the runner makes.
    """
    manifest_path = tmp_path / "manifest.jsonl"
    with open(manifest_path, "w") as f:
        for row in manifest_rows:
            f.write(json.dumps(row) + "\n")

    # Build a clock sequence that delivers the intended elapsed values
    # at the semantically meaningful call sites, filling intermediate
    # calls with the nearest elapsed value.  This removes the fragility
    # of counting exact perf_counter calls per trial.
    session_start = 0.0
    clock_values: list[float] = [session_start]  # session_started_monotonic
    for elapsed in admission_elapsed:
        # Each trial consumes ~10 perf_counter calls.  Pad with the
        # elapsed value so the completion-time call sees the intended
        # delta from session_start.
        clock_values.extend([session_start + elapsed] * 10)
    clock_iter = iter(clock_values)

    class _FakeStream:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"content-type": "text/event-stream"}
            self.http_version = "HTTP/1.1"

        def iter_lines(self):
            yield from _fake_chat_response("ok").decode().splitlines()

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP error")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _FakeResponse:
        def __init__(self, status_code: int = 200):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP error")

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            pass

        def stream(self, method: str, url: str, **kwargs):
            return _FakeStream(200)

        def get(self, url: str, **kwargs):
            return _FakeResponse(200)

    clock_iter = iter(clock_values)

    def fake_perf_counter() -> float:
        return next(clock_iter)

    with (
        mock.patch("traffic_measure.runner.httpx.Client", new=_FakeClient),
        mock.patch("traffic_measure.runner.time.perf_counter", new=fake_perf_counter),
    ):
        settings = RunSettings(
            manifest_path=manifest_path,
            output_dir=tmp_path / "run",
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            api_key="k",
            sample_limit=len({row["sample_id"] for row in manifest_rows}),
            repetitions=1,
            seed=42,
            temperature=0,
            max_output_tokens=16,
            request_timeout_seconds=30,
            observation_seconds=30,
            capture_interface="lo",
            capture_filter="tcp port 8000",
            capture_startup_delay_seconds=0,
            no_capture=True,
            no_wait_after_request=True,
            backend="local_vllm",
            transport=transport,
            connection_mode=connection_mode,
            session_budget_seconds=session_budget_seconds,
        )
        run_experiment(settings)

    results_path = tmp_path / "run" / "results.jsonl"
    if not results_path.exists():
        return 0
    completed = 0
    with open(results_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("completed"):
                completed += 1
    return completed


def test_admission_warmup_time_excluded():
    """Warm-up latency does not count toward the admission budget."""
    manifest = _fake_manifest(5)
    count = _run_with_clock(manifest, [0, 8, 16, 24, 34],
                            session_budget_seconds=30.0, tmp_path=Path(tempfile.mkdtemp()))
    assert count == 5  # first 4 < 30 admitted; 5th at 34s completes then stops


def test_completion_at_29_999_admits_next():
    """Completion at 29.999s admits another prompt."""
    manifest = _fake_manifest(5)
    count = _run_with_clock(manifest, [29.999, 37.0],
                            session_budget_seconds=30.0, tmp_path=Path(tempfile.mkdtemp()))
    assert count == 2


def test_completion_at_exactly_30_does_not_admit():
    """Completion at exactly 30.000s: no further admission."""
    manifest = _fake_manifest(5)
    count = _run_with_clock(manifest, [30.0],
                            session_budget_seconds=30.0, tmp_path=Path(tempfile.mkdtemp()))
    assert count == 1


def test_admitted_overrun_response_finishes():
    """Admitted response completes past 30s."""
    manifest = _fake_manifest(5)
    count = _run_with_clock(manifest, [5.0, 55.0],
                            session_budget_seconds=30.0, tmp_path=Path(tempfile.mkdtemp()))
    assert count == 2


def test_admission_with_warm_tls_transport():
    """Warm TLS setup excluded from admission budget."""
    manifest = _fake_manifest(5)
    count = _run_with_clock(manifest, [8.0, 16.0, 35.0],
                            session_budget_seconds=30.0, transport="tls13",
                            connection_mode="warm", tmp_path=Path(tempfile.mkdtemp()))
    assert count == 3
