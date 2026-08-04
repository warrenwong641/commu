from __future__ import annotations

from datetime import datetime, timedelta, timezone

from traffic_experiment.traffic_measure.session_timeline import Packet, build_timeline


def _utc(seconds: float) -> str:
    value = datetime(2026, 8, 4, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_long_response_is_indexed_across_30_second_segments():
    origin = datetime(2026, 8, 4, tzinfo=timezone.utc).timestamp()
    results = [
        {
            "completed": True,
            "session_id": "session-1",
            "request_id": "prompt-1",
            "condition": "no_compression",
            "started_at_utc": _utc(0),
            "finished_at_utc": _utc(65),
            "time_to_response_headers_seconds": 1.0,
            "time_to_first_content_seconds": 1.5,
            "request_json_bytes": 120000,
            "finish_reason": "stop",
            "output_tokens": 1024,
        },
        {
            "completed": True,
            "session_id": "session-1",
            "request_id": "prompt-2",
            "condition": "no_compression",
            "started_at_utc": _utc(65),
            "finished_at_utc": _utc(70),
            "time_to_response_headers_seconds": 0.5,
            "request_json_bytes": 80000,
            "finish_reason": "stop",
            "output_tokens": 100,
        },
    ]
    packets = [
        Packet(origin + 0.01, 160, "uplink", 100),
        Packet(origin + 0.20, 110, "uplink", 50),
        Packet(origin + 1.10, 260, "downlink", 200),
        Packet(origin + 31.00, 360, "downlink", 300),
        Packet(origin + 61.00, 460, "downlink", 400),
        Packet(origin + 65.10, 210, "uplink", 150),
        Packet(origin + 66.00, 260, "downlink", 200),
    ]

    segments, prompts = build_timeline(results, packets, segment_seconds=30)

    assert [row["segment_index"] for row in segments] == [1, 2, 3]
    assert [row["segment_duration_seconds"] for row in segments] == [30, 30, 10]
    assert segments[0]["prompt_indices_started"] == "1"
    assert segments[2]["prompt_indices_started"] == "2"
    assert segments[2]["active_response_prompt_indices"] == "1;2"
    assert prompts[0]["prompt_sent_at_utc"] == _utc(0)
    assert prompts[0]["prompt_segment_index"] == 1
    assert prompts[0]["response_end_segment_index"] == 3
    assert prompts[0]["response_segments_spanned"] == 3
    assert prompts[0]["wire_upload_payload_bytes_before_response"] == 150
    assert prompts[1]["prompt_segment_index"] == 3
