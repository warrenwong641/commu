from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from traffic_experiment.traffic_measure.backends import (
    build_backend_request,
    normalized_usage,
    parse_gemini_sse,
    parse_openai_sse,
)
from traffic_experiment.traffic_measure.capture import CaptureResult
from traffic_experiment.traffic_measure.common import read_jsonl, write_jsonl
from traffic_experiment.traffic_measure.prepare import (
    _load_compressor,
    merge_manifest_shards,
    prepare_manifest,
    prepare_summary_manifest,
)
from traffic_experiment.traffic_measure.runner import (
    RunSettings,
    _capture_attempt_counts,
    _job_id,
    _prior_progress,
    _request_once_curl,
    _request_once_http3,
    _trial_rows,
    _warm_http3_client,
    parse_sse_lines,
    run_experiment,
)
from traffic_experiment.traffic_measure.http3_client import Http3Response


def _write_minimal_locomo(path: Path, count: int = 1) -> None:
    raw = [
        {
            "sample_id": f"conversation-{index}",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1": [
                    {"speaker": "Alice", "dia_id": f"D{index}:1", "text": "I moved to Taipei."},
                    {"speaker": "Bob", "dia_id": f"D{index}:2", "text": "How exciting!"},
                ],
            },
            "qa": [
                {
                    "question_id": "q1",
                    "question": "Where did Alice move?",
                    "answer": "Taipei",
                    "evidence": [f"D{index}:1"],
                    "category": 2,
                }
            ],
        }
        for index in range(1, count + 1)
    ]
    path.mkdir(parents=True)
    (path / "locomo.json").write_text(json.dumps(raw), encoding="utf-8")


def test_prepare_manifest_without_compressor(tmp_path):
    data_dir = tmp_path / "data"
    output = tmp_path / "requests.jsonl"
    _write_minimal_locomo(data_dir)

    rows = prepare_manifest(
        data_dir=data_dir,
        output_path=output,
        sample_count=1,
        seed=42,
        conditions=["no_compression"],
        compressor_model="unused",
        compressor_device="cpu",
    )

    assert len(rows) == 1
    assert rows[0]["request_id"] == "conversation-1::q1::no_compression"
    assert rows[0]["messages_sha256"]
    assert read_jsonl(output) == rows

    with pytest.raises(FileExistsError, match="refusing to overwrite frozen manifest"):
        prepare_manifest(
            data_dir=data_dir,
            output_path=output,
            sample_count=1,
            seed=42,
            conditions=["no_compression"],
            compressor_model="unused",
            compressor_device="cpu",
        )


def test_missing_compressor_error_points_to_locked_environment(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "llmlingua", None)

    with pytest.raises(RuntimeError) as error:
        _load_compressor("unused", "cpu")

    message = str(error.value)
    assert "traffic_experiment/scripts/01_setup_runner.sh" in message
    assert "traffic_experiment/.venv-compression/bin/python" in message
    assert "requirements-compression.txt" not in message


def test_prepare_manifest_shards_merge_in_original_order(tmp_path):
    data_dir = tmp_path / "data"
    shard_zero = tmp_path / "shard-zero.jsonl"
    shard_one = tmp_path / "shard-one.jsonl"
    merged = tmp_path / "merged.jsonl"
    _write_minimal_locomo(data_dir, count=4)

    for shard_index, output in enumerate((shard_zero, shard_one)):
        prepare_manifest(
            data_dir=data_dir,
            output_path=output,
            sample_count=4,
            seed=42,
            conditions=["no_compression"],
            compressor_model="unused",
            compressor_device="cpu",
            shard_count=2,
            shard_index=shard_index,
        )

    rows = merge_manifest_shards(
        input_paths=[shard_zero, shard_one],
        output_path=merged,
        expected_rows=4,
    )

    assert [row["selection_index"] for row in rows] == [0, 1, 2, 3]
    assert len({row["request_id"] for row in rows}) == 4
    assert read_jsonl(merged) == rows


def test_prepare_event_summary_manifest(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    raw = [
        {
            "sample_id": "conversation-summary",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1": [
                    {"speaker": "Alice", "dia_id": "D1:1", "text": "I moved to Taipei."},
                    {"speaker": "Bob", "dia_id": "D1:2", "text": "I started a new job."},
                ],
            },
            "event_summary": {
                "events_session_1": {
                    "Alice": ["Alice moved to Taipei."],
                    "Bob": ["Bob started a new job."],
                }
            },
            "qa": [],
        }
    ]
    (data_dir / "locomo.json").write_text(json.dumps(raw), encoding="utf-8")

    rows = prepare_summary_manifest(
        data_dir=data_dir,
        output_path=tmp_path / "summaries.jsonl",
        conversation_count=1,
        seed=42,
        conditions=["no_compression"],
        compressor_model="unused",
        compressor_device="cpu",
    )

    assert len(rows) == 2
    assert {row["target_speaker"] for row in rows} == {"Alice", "Bob"}
    assert all(row["task_type"] == "event_summary" for row in rows)
    assert "moved to Taipei" in next(row for row in rows if row["target_speaker"] == "Alice")[
        "reference_answer"
    ]


def test_parse_streaming_response():
    lines = [
        'data: {"id":"abc","choices":[{"delta":{"content":"Tai"}}]}',
        'data: {"id":"abc","choices":[{"delta":{"content":"pei"}}]}',
        'data: {"id":"abc","choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"id":"abc","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    text, usage, response_id = parse_sse_lines(lines)
    assert text == "Taipei"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2}
    assert response_id == "abc"
    assert parse_openai_sse(lines).finish_reason == "stop"


def test_gemini_request_and_stream_parsing():
    request = build_backend_request(
        backend="gemini",
        base_url="https://example.test/v1beta",
        model="gemini-test",
        api_key="secret",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Where?"},
        ],
        generation={"temperature": 0, "max_tokens": 16, "stream": True},
    )
    assert request.endpoint.endswith("streamGenerateContent?alt=sse")
    assert request.headers == {"x-goog-api-key": "secret"}
    assert request.payload["systemInstruction"]["parts"][0]["text"] == "Be concise."
    parsed = parse_gemini_sse(
        [
            'data: {"responseId":"g1","modelVersion":"v1","candidates":[{"content":{"parts":[{"text":"Tai"}]}}]}',
            'data: {"candidates":[{"content":{"parts":[{"text":"pei"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":2}}',
        ]
    )
    assert parsed.text == "Taipei"
    assert parsed.finish_reason == "STOP"
    assert normalized_usage("gemini", parsed.usage) == (10, 2)


def test_openrouter_provider_is_pinned():
    request = build_backend_request(
        backend="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="qwen/test",
        api_key="secret",
        messages=[{"role": "user", "content": "Hello"}],
        generation={"temperature": 0, "max_tokens": 16, "stream": True},
        openrouter_provider="ProviderName",
    )
    assert request.payload["provider"] == {
        "only": ["ProviderName"],
        "allow_fallbacks": False,
    }


def test_trial_workers_are_disjoint_balanced_and_keep_sample_conditions_together():
    manifest = [
        {
            "sample_id": f"sample-{sample}",
            "request_id": f"sample-{sample}::{condition}",
            "condition": condition,
        }
        for sample in range(8)
        for condition in ("no_compression", "longllmlingua_2x", "longllmlingua_4x")
    ]
    worker_trials = [
        _trial_rows(
            manifest,
            sample_limit=8,
            repetitions=3,
            seed=42,
            worker_count=2,
            worker_index=worker,
        )
        for worker in range(2)
    ]

    keys = [
        {(row["request_id"], repetition) for row, repetition in trials}
        for trials in worker_trials
    ]
    sample_sets = [
        {row["sample_id"] for row, _ in trials}
        for trials in worker_trials
    ]
    assert len(worker_trials[0]) == len(worker_trials[1]) == 36
    assert keys[0].isdisjoint(keys[1])
    assert sample_sets[0].isdisjoint(sample_sets[1])
    assert len(sample_sets[0] | sample_sets[1]) == 8


def test_job_id_is_deterministic_and_repetition_specific():
    assert _job_id("sample::no_compression", 1) == _job_id(
        "sample::no_compression", 1
    )


def test_http3_warmup_reuses_healthy_client():
    class Client:
        closed = False

        def get(self, url, headers, timeout_seconds):
            assert url.endswith("/models")
            assert headers == {"Authorization": "Bearer key"}
            assert timeout_seconds == 5
            return type("Response", (), {"status": 200})()

        def close(self):
            self.closed = True

    client = Client()
    assert _warm_http3_client(client, "https://example.test/v1", "key", None) is client
    assert client.closed is False


def test_http3_warmup_replaces_dead_client(monkeypatch):
    class DeadClient:
        closed = False

        def get(self, url, headers, timeout_seconds):
            raise TimeoutError("closed QUIC connection")

        def close(self):
            self.closed = True

    class Replacement:
        def __init__(self, base_url, ca_file):
            self.base_url = base_url
            self.ca_file = ca_file

        def get(self, url, headers, timeout_seconds):
            return type("Response", (), {"status": 200})()

        def close(self):
            raise AssertionError("healthy replacement should remain open")

    dead = DeadClient()
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.PersistentHttp3Client",
        Replacement,
    )
    replacement = _warm_http3_client(
        dead,
        "https://example.test/v1",
        "key",
        Path("ca.pem"),
    )
    assert dead.closed is True
    assert isinstance(replacement, Replacement)
    assert _job_id("sample::no_compression", 1) != _job_id(
        "sample::no_compression", 2
    )


class _VllmLikeHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        assert payload["stream"] is True
        body = "\n\n".join(
            [
                'data: {"id":"response-1","choices":[{"delta":{"content":"Taipei"}}]}',
                'data: {"id":"response-1","choices":[{"delta":{},"finish_reason":"stop"}]}',
                'data: {"id":"response-1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2}}',
                "data: [DONE]",
                "",
            ]
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


def test_runner_against_mock_streaming_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmLikeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = tmp_path / "manifest.jsonl"
        messages = [{"role": "user", "content": "Where?"}]
        write_jsonl(
            manifest,
            [
                {
                    "request_id": "conversation-1::q1::no_compression",
                    "sample_id": "conversation-1::q1",
                    "conversation_id": "conversation-1",
                    "question_id": "q1",
                    "condition": "no_compression",
                    "messages": messages,
                    "messages_sha256": "a" * 64,
                }
            ],
        )
        output_dir = tmp_path / "run"
        results_path = run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=output_dir,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=1,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=0,
                capture_interface="",
                capture_filter="",
                capture_startup_delay_seconds=0,
                worker_gpu_index=7,
                worker_gpu_uuid="GPU-test-uuid",
                no_capture=True,
                no_wait_after_request=True,
            )
        )
        result = read_jsonl(results_path)[0]
        assert result["completed"] is True
        assert result["worker_gpu_index"] == 7
        assert result["worker_gpu_uuid"] == "GPU-test-uuid"
        assert result["capture_may_be_truncated"] is None
        assert result["response_text"] == "Taipei"
        assert result["finish_reason"] == "stop"
        assert result["input_tokens"] == 12
        assert result["output_tokens"] == 2
        assert result["request_json_bytes"] > 0
        assert result["response_sse_bytes"] > 0
        assert result["sse_event_count"] == 4
        assert result["content_event_count"] == 1
        assert result["time_to_response_headers_seconds"] >= 0
        assert result["time_to_first_content_seconds"] >= 0
        assert result["post_first_content_tokens_per_second"] > 0
    finally:
        server.shutdown()
        server.server_close()


def test_capture_failure_is_retried_as_distinct_preserved_attempt(
    tmp_path,
    monkeypatch,
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmLikeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    outcomes = iter([2, 0])

    class FakeCapture:
        def __init__(self, output_path, **_kwargs):
            self.output_path = output_path

        def start(self):
            return None

        def finish(self):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b"pcap-attempt")
            return_code = next(outcomes)
            return CaptureResult(
                path=self.output_path,
                return_code=return_code,
                stderr="capture failed" if return_code else "",
            )

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.DumpcapCapture",
        FakeCapture,
    )
    try:
        manifest = tmp_path / "manifest.jsonl"
        messages = [{"role": "user", "content": "Where?"}]
        write_jsonl(
            manifest,
            [
                {
                    "request_id": "conversation-1::q1::no_compression",
                    "sample_id": "conversation-1::q1",
                    "conversation_id": "conversation-1",
                    "question_id": "q1",
                    "condition": "no_compression",
                    "messages": messages,
                    "messages_sha256": "a" * 64,
                }
            ],
        )
        settings = RunSettings(
            manifest_path=manifest,
            output_dir=tmp_path / "run",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="test-model",
            api_key="test-key",
            sample_limit=1,
            repetitions=1,
            seed=42,
            temperature=0,
            max_output_tokens=16,
            request_timeout_seconds=5,
            observation_seconds=1,
            capture_interface="lo",
            capture_filter="tcp port 8000",
            capture_startup_delay_seconds=0,
        )

        results_path = run_experiment(settings)
        first = read_jsonl(results_path)[0]
        first_capture = Path(first["capture_file"])
        assert first["attempt"] == 1
        assert first["completed"] is False
        assert first["capture_return_code"] == 2
        assert first["capture_sha256"]
        assert "CaptureError" in first["error"]
        assert first_capture.name.endswith(".partial.pcapng")
        assert first_capture.exists()

        run_experiment(settings)
        rows = read_jsonl(results_path)
        second = rows[1]
        assert second["attempt"] == 2
        assert second["attempt_id"] != first["attempt_id"]
        assert second["completed"] is True
        assert second["capture_return_code"] == 0
        assert Path(second["capture_file"]).exists()
        assert Path(second["capture_file"]) != first_capture
        assert first_capture.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_interrupted_capture_without_result_row_advances_attempt(
    tmp_path,
    monkeypatch,
):
    request_id = "conversation-1::q1::no_compression"
    job_id = _job_id(request_id, 1)
    output_dir = tmp_path / "run"
    captures_dir = output_dir / "captures"
    captures_dir.mkdir(parents=True)
    interrupted = captures_dir / f"{job_id}-attempt-001-1234abcd.partial.pcapng"
    interrupted.write_bytes(b"interrupted-capture")
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {
                "request_id": request_id,
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner._request_once",
        lambda *_args, **_kwargs: {
            "response_text": "Taipei",
            "elapsed_seconds": 0.1,
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        },
    )

    results_path = run_experiment(
        RunSettings(
            manifest_path=manifest,
            output_dir=output_dir,
            base_url="http://127.0.0.1:9/v1",
            model="test-model",
            api_key="test-key",
            sample_limit=1,
            repetitions=1,
            seed=42,
            temperature=0,
            max_output_tokens=16,
            request_timeout_seconds=5,
            observation_seconds=0,
            capture_interface="",
            capture_filter="",
            capture_startup_delay_seconds=0,
            no_capture=True,
            no_wait_after_request=True,
        )
    )

    result = read_jsonl(results_path)[0]
    assert result["attempt"] == 2
    assert f"{job_id}-attempt-002-" in result["attempt_id"]
    assert interrupted.exists()


def test_capture_inventory_counts_finalized_and_partial_files(tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    first_job = "1" * 24
    second_job = "2" * 24
    (captures_dir / f"{first_job}-attempt-001-1234abcd.pcapng").write_bytes(
        b"one"
    )
    (
        captures_dir / f"{first_job}-attempt-003-2345bcde.partial.pcapng"
    ).write_bytes(b"three")
    (captures_dir / f"{second_job}-attempt-002-3456cdef.pcapng").write_bytes(
        b"two"
    )

    assert _capture_attempt_counts(captures_dir) == {
        first_job: 3,
        second_job: 2,
    }


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        f"{'1' * 24}-attempt-000-1234abcd.partial.pcapng",
        f"{'1' * 24}-attempt-0001-1234abcd.pcapng",
    ],
)
def test_capture_inventory_rejects_unrecognized_or_noncanonical_names(
    tmp_path,
    name,
):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    (captures_dir / name).write_bytes(b"unsafe")

    with pytest.raises(ValueError, match="capture (inventory entry|attempt)"):
        _capture_attempt_counts(captures_dir)


def test_capture_inventory_rejects_symlink(tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    target = tmp_path / "elsewhere.pcapng"
    target.write_bytes(b"outside")
    link = captures_dir / f"{'1' * 24}-attempt-001-1234abcd.pcapng"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="unsafe capture inventory entry"):
        _capture_attempt_counts(captures_dir)


def test_capture_constructor_failure_is_recorded_without_observation_wait(
    tmp_path,
    monkeypatch,
):
    sleeps = []

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("request must not start after capture setup fails")

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner._request_once",
        unexpected_request,
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.time.sleep",
        sleeps.append,
    )
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {
                "request_id": "conversation-1::q1::no_compression",
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )
    result = read_jsonl(
        run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=tmp_path / "run",
                base_url="http://127.0.0.1:9/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=1,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=900,
                capture_interface="",
                capture_filter="tcp port 8000",
                capture_startup_delay_seconds=0,
            )
        )
    )[0]

    assert sleeps == []
    assert result["completed"] is False
    assert result["capture_file"] is None
    assert result["error"].startswith("ValueError: capture interface is required")


def test_intentional_no_capture_preserves_observation_wait(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner._request_once",
        lambda *_args, **_kwargs: {
            "response_text": "Taipei",
            "elapsed_seconds": 0.1,
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        },
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.time.sleep",
        sleeps.append,
    )
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {
                "request_id": "conversation-1::q1::no_compression",
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )
    result = read_jsonl(
        run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=tmp_path / "run",
                base_url="http://127.0.0.1:9/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=1,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=900,
                capture_interface="",
                capture_filter="",
                capture_startup_delay_seconds=0,
                no_capture=True,
            )
        )
    )[0]

    assert sleeps == [900]
    assert result["completed"] is True
    assert result["capture_file"] is None
    assert result["error"] is None


@pytest.mark.parametrize(
    ("capture_stop_on_response", "startup_delay_seconds", "response_elapsed_seconds"),
    [
        (True, 0.0, 10.0),
        (False, 2.0, 8.0),
    ],
)
def test_capture_observation_ceiling_is_failed_and_retried(
    tmp_path,
    monkeypatch,
    capture_stop_on_response,
    startup_delay_seconds,
    response_elapsed_seconds,
):
    class FakeCapture:
        def __init__(self, output_path, **_kwargs):
            self.output_path = output_path

        def start(self):
            return None

        def finish(self):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b"complete-looking-but-truncated")
            return CaptureResult(
                path=self.output_path,
                return_code=0,
                stderr="",
            )

    def fake_request_once(_client, _request, _backend):
        return {
            "response_text": "Taipei",
            "elapsed_seconds": response_elapsed_seconds,
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        }

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.DumpcapCapture",
        FakeCapture,
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner._request_once",
        fake_request_once,
    )

    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {
                "request_id": "conversation-1::q1::no_compression",
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )
    settings = RunSettings(
        manifest_path=manifest,
        output_dir=tmp_path / "run",
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key="test-key",
        sample_limit=1,
        repetitions=1,
        seed=42,
        temperature=0,
        max_output_tokens=16,
        request_timeout_seconds=5,
        observation_seconds=10,
        capture_interface="lo",
        capture_filter="tcp port 8000",
        capture_startup_delay_seconds=startup_delay_seconds,
        capture_stop_on_response=capture_stop_on_response,
    )

    results_path = run_experiment(settings)
    run_experiment(settings)
    rows = read_jsonl(results_path)

    assert [row["attempt"] for row in rows] == [1, 2]
    assert rows[0]["attempt_id"] != rows[1]["attempt_id"]
    for row in rows:
        capture_path = Path(row["capture_file"])
        assert row["completed"] is False
        assert row["capture_may_be_truncated"] is True
        assert row["capture_startup_delay_seconds"] == startup_delay_seconds
        assert row["capture_sha256"]
        assert row["error"].startswith("CaptureError:")
        assert "observation ceiling" in row["error"]
        assert capture_path.name.endswith(".partial.pcapng")
        assert capture_path.exists()


def test_capture_with_headroom_completes(tmp_path, monkeypatch):
    class FakeCapture:
        def __init__(self, output_path, **_kwargs):
            self.output_path = output_path

        def start(self):
            return None

        def finish(self):
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b"complete-capture")
            return CaptureResult(
                path=self.output_path,
                return_code=0,
                stderr="",
            )

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.DumpcapCapture",
        FakeCapture,
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner._request_once",
        lambda _client, _request, _backend: {
            "response_text": "Taipei",
            "elapsed_seconds": 7.5,
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        },
    )
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(
        manifest,
        [
            {
                "request_id": "conversation-1::q1::no_compression",
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )

    result = read_jsonl(
        run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=tmp_path / "run",
                base_url="http://127.0.0.1:8000/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=1,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=10,
                capture_interface="lo",
                capture_filter="tcp port 8000",
                capture_startup_delay_seconds=2.0,
                capture_stop_on_response=True,
            )
        )
    )[0]

    capture_path = Path(result["capture_file"])
    assert result["completed"] is True
    assert result["capture_may_be_truncated"] is False
    assert result["error"] is None
    assert capture_path.name.endswith(".pcapng")
    assert not capture_path.name.endswith(".partial.pcapng")
    assert capture_path.exists()


def test_http3_request_records_symmetric_stream_metrics():
    backend_request = build_backend_request(
        backend="local_vllm",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
        messages=[{"role": "user", "content": "Where?"}],
        generation={"temperature": 0, "max_tokens": 16, "stream": True},
    )

    class FakeClient:
        def post(self, endpoint, headers, payload, timeout_seconds):
            assert endpoint == backend_request.endpoint
            assert payload == backend_request.payload
            return Http3Response(
                status=200,
                headers={"content-type": "text/event-stream"},
                body="\n\n".join(
                    [
                        'data: {"choices":[{"delta":{"content":"Taipei"}}]}',
                        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2}}',
                        "data: [DONE]",
                        "",
                    ]
                ),
                time_to_first_byte_seconds=0.1,
                response_body_bytes=240,
                sse_event_count=4,
                content_event_offsets_seconds=(0.2,),
            )

    result = _request_once_http3(
        backend_request,
        "local_vllm",
        5,
        None,
        client=FakeClient(),
    )
    assert result["request_json_bytes"] > 0
    assert result["response_sse_bytes"] == 240
    assert result["sse_event_count"] == 4
    assert result["content_event_count"] == 1
    assert result["time_to_first_content_seconds"] == 0.2
    assert result["finish_reason"] == "stop"


def test_curl_request_passes_authorization_via_stdin_not_argv(monkeypatch):
    request = build_backend_request(
        backend="local_vllm",
        base_url="https://example.test/v1",
        model="test-model",
        api_key='secret-"value',
        messages=[{"role": "user", "content": "Where?"}],
        generation={"temperature": 0, "max_tokens": 16, "stream": True},
    )
    invocation = {}

    def fake_run(command, **kwargs):
        invocation["command"] = command
        invocation["input"] = kwargs["input"]
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stderr": "",
                "stdout": (
                    'data: {"choices":[{"delta":{"content":"Taipei"}}]}\n'
                    "data: [DONE]\n"
                    "\n__TRAFFIC_META__200,1.1,0.125\n"
                ),
            },
        )()

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.runner.subprocess.run",
        fake_run,
    )
    result = _request_once_curl(
        request,
        backend="local_vllm",
        transport="tls13",
        timeout_seconds=5,
        curl_executable="curl",
        tls_ca_file=None,
    )

    command_text = "\0".join(invocation["command"])
    assert "secret-" not in command_text
    assert "Authorization:" not in command_text
    assert invocation["command"][1:3] == ["--config", "-"]
    assert 'Authorization: Bearer secret-\\"value' in invocation["input"]
    assert result["response_text"] == "Taipei"


def test_runner_filters_to_one_compression_condition(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmLikeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = tmp_path / "manifest.jsonl"
        rows = []
        for condition in ("no_compression", "longllmlingua_2x"):
            rows.append(
                {
                    "request_id": f"conversation-1::q1::{condition}",
                    "sample_id": "conversation-1::q1",
                    "conversation_id": "conversation-1",
                    "question_id": "q1",
                    "condition": condition,
                    "messages": [{"role": "user", "content": "Where?"}],
                    "messages_sha256": condition,
                }
            )
        write_jsonl(manifest, rows)
        results_path = run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=tmp_path / "run",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=1,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=0,
                capture_interface="",
                capture_filter="",
                capture_startup_delay_seconds=0,
                no_capture=True,
                no_wait_after_request=True,
                condition="no_compression",
            )
        )
        results = read_jsonl(results_path)
        assert len(results) == 1
        assert results[0]["condition"] == "no_compression"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "ledger_schema",
    ["commu-matrix-parent-ledger-v1", "commu-matrix-parent-ledger-v2"],
)
def test_parent_ledger_skips_completed_and_counts_orphan_attempt(
    tmp_path, ledger_schema
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmLikeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = tmp_path / "manifest.jsonl"
        request_ids = [
            "conversation-1::q1::no_compression",
            "conversation-2::q1::no_compression",
        ]
        write_jsonl(
            manifest,
            [
                {
                    "request_id": request_id,
                    "sample_id": f"conversation-{index}::q1",
                    "conversation_id": f"conversation-{index}",
                    "question_id": "q1",
                    "condition": "no_compression",
                    "messages": [{"role": "user", "content": "Where?"}],
                    "messages_sha256": str(index) * 64,
                }
                for index, request_id in enumerate(request_ids, 1)
            ],
        )
        orphan_job = _job_id(request_ids[1], 1)
        ledger = tmp_path / "PARENT_LEDGER.json"
        ledger.write_text(
            json.dumps(
                {
                    "schema": ledger_schema,
                    "cells": {
                        "rtt/qa/tls13": {
                            "completed": [
                                {"request_id": request_ids[0], "repetition": 1}
                            ],
                            "attempt_counts": [
                                {
                                    "request_id": request_ids[1],
                                    "repetition": 1,
                                    "count": 2,
                                }
                            ],
                            "orphan_attempt_counts": [
                                {"job_id": orphan_job, "count": 3}
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        results_path = run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=tmp_path / "target",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=2,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=0,
                capture_interface="",
                capture_filter="",
                capture_startup_delay_seconds=0,
                no_capture=True,
                no_wait_after_request=True,
                prior_ledger_path=ledger,
                prior_ledger_cell="rtt/qa/tls13",
            )
        )
        rows = read_jsonl(results_path)
        assert len(rows) == 1
        assert rows[0]["request_id"] == request_ids[1]
        assert rows[0]["attempt"] == 4
        assert "-attempt-004-" in rows[0]["attempt_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_parent_ledger_rejects_unknown_schema(tmp_path):
    ledger = tmp_path / "PARENT_LEDGER.json"
    ledger.write_text(
        json.dumps(
            {
                "schema": "commu-matrix-parent-ledger-v3",
                "cells": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported schema"):
        _prior_progress(ledger, "baseline/qa/tls13")


@pytest.mark.parametrize(
    "ledger_schema",
    ["commu-matrix-parent-ledger-v1", "commu-matrix-parent-ledger-v2"],
)
def test_parent_ledger_rejects_target_completed_overlap(tmp_path, ledger_schema):
    manifest = tmp_path / "manifest.jsonl"
    request_id = "conversation-1::q1::no_compression"
    write_jsonl(
        manifest,
        [
            {
                "request_id": request_id,
                "sample_id": "conversation-1::q1",
                "conversation_id": "conversation-1",
                "question_id": "q1",
                "condition": "no_compression",
                "messages": [{"role": "user", "content": "Where?"}],
                "messages_sha256": "a" * 64,
            }
        ],
    )
    output = tmp_path / "target"
    output.mkdir()
    write_jsonl(
        output / "results.jsonl",
        [{"request_id": request_id, "repetition": 1, "completed": True}],
    )
    ledger = tmp_path / "PARENT_LEDGER.json"
    ledger.write_text(
        json.dumps(
            {
                "schema": ledger_schema,
                "cells": {
                    "baseline/qa/tls13": {
                        "completed": [{"request_id": request_id, "repetition": 1}],
                        "attempt_counts": [],
                        "orphan_attempt_counts": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings = RunSettings(
        manifest_path=manifest,
        output_dir=output,
        base_url="http://127.0.0.1:1/v1",
        model="test-model",
        api_key="test-key",
        sample_limit=1,
        repetitions=1,
        seed=42,
        temperature=0,
        max_output_tokens=16,
        request_timeout_seconds=5,
        observation_seconds=0,
        capture_interface="",
        capture_filter="",
        capture_startup_delay_seconds=0,
        no_capture=True,
        no_wait_after_request=True,
        prior_ledger_path=ledger,
        prior_ledger_cell="baseline/qa/tls13",
    )
    with pytest.raises(ValueError, match="overlap immutable parent"):
        run_experiment(settings)


def test_session_budget_finishes_first_response_but_admits_no_late_second(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmLikeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = tmp_path / "manifest.jsonl"
        rows = []
        for index in range(2):
            messages = [{"role": "user", "content": f"Question {index}?"}]
            rows.append(
                {
                    "request_id": f"conversation-{index}::q1::no_compression",
                    "sample_id": f"conversation-{index}::q1",
                    "conversation_id": f"conversation-{index}",
                    "question_id": "q1",
                    "condition": "no_compression",
                    "messages": messages,
                    "messages_sha256": str(index) * 64,
                }
            )
        write_jsonl(manifest, rows)
        output_dir = tmp_path / "session"
        results_path = run_experiment(
            RunSettings(
                manifest_path=manifest,
                output_dir=output_dir,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                model="test-model",
                api_key="test-key",
                sample_limit=2,
                repetitions=1,
                seed=42,
                temperature=0,
                max_output_tokens=16,
                request_timeout_seconds=5,
                observation_seconds=0,
                capture_interface="",
                capture_filter="",
                capture_startup_delay_seconds=0,
                no_capture=True,
                no_wait_after_request=True,
                session_id="budget-test",
                session_budget_seconds=0.000001,
            )
        )
        results = read_jsonl(results_path)
        assert len(results) == 1
        assert results[0]["completed"] is True
        assert results[0]["session_budget_exceeded_on_completion"] is True
    finally:
        server.shutdown()
        server.server_close()
