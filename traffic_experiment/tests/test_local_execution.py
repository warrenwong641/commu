from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from traffic_experiment.traffic_measure.common import read_jsonl, write_jsonl
from traffic_experiment.traffic_measure.prepare import prepare_manifest
from traffic_experiment.traffic_measure.runner import RunSettings, parse_sse_lines, run_experiment


def _write_minimal_locomo(path: Path) -> None:
    raw = [
        {
            "sample_id": "conversation-1",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1": [
                    {"speaker": "Alice", "dia_id": "D1:1", "text": "I moved to Taipei."},
                    {"speaker": "Bob", "dia_id": "D1:2", "text": "How exciting!"},
                ],
            },
            "qa": [
                {
                    "question_id": "q1",
                    "question": "Where did Alice move?",
                    "answer": "Taipei",
                    "evidence": ["D1:1"],
                    "category": 2,
                }
            ],
        }
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
