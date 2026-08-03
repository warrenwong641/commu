from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from traffic_experiment.traffic_measure.common import read_jsonl, write_jsonl
from traffic_experiment.traffic_measure.prepare import merge_manifest_shards, prepare_manifest
from traffic_experiment.traffic_measure.runner import (
    RunSettings,
    _trial_rows,
    parse_sse_lines,
    run_experiment,
)


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


def test_parse_streaming_response():
    text, usage, response_id = parse_sse_lines(
        [
            'data: {"id":"abc","choices":[{"delta":{"content":"Tai"}}]}',
            'data: {"id":"abc","choices":[{"delta":{"content":"pei"}}]}',
            'data: {"id":"abc","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
            "data: [DONE]",
        ]
    )
    assert text == "Taipei"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2}
    assert response_id == "abc"


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


class _VllmLikeHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        assert payload["stream"] is True
        body = "\n\n".join(
            [
                'data: {"id":"response-1","choices":[{"delta":{"content":"Taipei"}}]}',
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
                no_capture=True,
                no_wait_after_request=True,
            )
        )
        result = read_jsonl(results_path)[0]
        assert result["completed"] is True
        assert result["response_text"] == "Taipei"
        assert result["input_tokens"] == 12
        assert result["output_tokens"] == 2
    finally:
        server.shutdown()
        server.server_close()
