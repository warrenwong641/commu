from __future__ import annotations

import csv
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import read_jsonl


@dataclass(frozen=True)
class Packet:
    epoch_seconds: float
    frame_bytes: int
    direction: str
    payload_bytes: int


def _parse_utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _format_utc(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _number(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_packets(
    capture_path: Path,
    server_port: int,
    transport: str,
    tshark: str = "tshark",
) -> list[Packet]:
    if shutil.which(tshark) is None:
        raise FileNotFoundError(f"{tshark} was not found in PATH")
    if transport not in {"tls13", "http3"}:
        raise ValueError("transport must be tls13 or http3")
    command = [
        tshark,
        "-r",
        str(capture_path),
        "-Y",
        "tcp || udp",
        "-T",
        "fields",
        "-E",
        "separator=/t",
        "-E",
        "quote=n",
        "-e",
        "frame.time_epoch",
        "-e",
        "frame.len",
        "-e",
        "tcp.srcport",
        "-e",
        "tcp.dstport",
        "-e",
        "tcp.len",
        "-e",
        "udp.srcport",
        "-e",
        "udp.dstport",
        "-e",
        "udp.length",
    ]
    process = subprocess.run(command, check=True, capture_output=True, text=True)
    packets: list[Packet] = []
    for raw_line in process.stdout.splitlines():
        fields = (raw_line.split("\t") + [""] * 8)[:8]
        epoch, frame_len, tcp_src, tcp_dst, tcp_len, udp_src, udp_dst, udp_len = fields
        try:
            epoch_seconds = float(epoch)
        except ValueError:
            continue
        if transport == "http3":
            src_port, dst_port = _number(udp_src), _number(udp_dst)
            payload_bytes = max(0, _number(udp_len) - 8)
        else:
            src_port, dst_port = _number(tcp_src), _number(tcp_dst)
            payload_bytes = _number(tcp_len)
        if dst_port == server_port:
            direction = "uplink"
        elif src_port == server_port:
            direction = "downlink"
        else:
            continue
        packets.append(
            Packet(
                epoch_seconds=epoch_seconds,
                frame_bytes=_number(frame_len),
                direction=direction,
                payload_bytes=payload_bytes,
            )
        )
    return packets


def _first_response_epoch(result: dict[str, Any], started: float) -> float | None:
    first_byte = result.get("first_byte_at_utc")
    if first_byte:
        return _parse_utc(str(first_byte))
    for field in ("time_to_response_headers_seconds", "time_to_first_byte_seconds"):
        value = result.get(field)
        if isinstance(value, (int, float)):
            return started + float(value)
    return None


def build_timeline(
    results: list[dict[str, Any]],
    packets: list[Packet],
    segment_seconds: float = 30.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be positive")
    packets = sorted(packets, key=lambda packet: packet.epoch_seconds)
    completed = [
        row
        for row in results
        if row.get("completed")
        and row.get("started_at_utc")
        and row.get("finished_at_utc")
    ]
    if not completed:
        raise ValueError("no completed timestamped requests were found")
    completed.sort(key=lambda row: _parse_utc(str(row["started_at_utc"])))
    origin = _parse_utc(str(completed[0]["started_at_utc"]))
    session_end = max(_parse_utc(str(row["finished_at_utc"])) for row in completed)
    session_duration = max(0.0, session_end - origin)
    segment_count = max(1, math.ceil(session_duration / segment_seconds))

    prompt_rows: list[dict[str, Any]] = []
    prompt_intervals: list[tuple[int, float, float]] = []
    for prompt_index, result in enumerate(completed, start=1):
        started = _parse_utc(str(result["started_at_utc"]))
        finished = _parse_utc(str(result["finished_at_utc"]))
        first_response = _first_response_epoch(result, started)
        first_content = result.get("first_token_at_utc")
        first_content_epoch = _parse_utc(str(first_content)) if first_content else None
        if first_content_epoch is None and isinstance(
            result.get("time_to_first_content_seconds"), (int, float)
        ):
            first_content_epoch = started + float(result["time_to_first_content_seconds"])

        upload_deadline = first_response if first_response is not None else finished
        upload_packets = [
            packet
            for packet in packets
            if packet.direction == "uplink"
            and packet.payload_bytes > 0
            and started <= packet.epoch_seconds <= upload_deadline
        ]
        start_offset = max(0.0, started - origin)
        finish_offset = max(start_offset, finished - origin)
        start_segment = int(start_offset // segment_seconds) + 1
        end_segment = int(max(0.0, finish_offset - 1e-9) // segment_seconds) + 1
        prompt_rows.append(
            {
                "session_id": result.get("session_id"),
                "prompt_index": prompt_index,
                "request_id": result.get("request_id"),
                "condition": result.get("condition"),
                "prompt_sent_at_utc": result["started_at_utc"],
                "prompt_sent_offset_seconds": round(start_offset, 6),
                "prompt_segment_index": start_segment,
                "logical_prompt_upload_bytes": result.get("request_json_bytes"),
                "wire_upload_first_packet_at_utc": (
                    _format_utc(upload_packets[0].epoch_seconds)
                    if upload_packets
                    else None
                ),
                "wire_upload_last_packet_at_utc": (
                    _format_utc(upload_packets[-1].epoch_seconds)
                    if upload_packets
                    else None
                ),
                "wire_upload_payload_bytes_before_response": sum(
                    packet.payload_bytes for packet in upload_packets
                ),
                "wire_upload_frame_bytes_before_response": sum(
                    packet.frame_bytes for packet in upload_packets
                ),
                "first_response_at_utc": (
                    _format_utc(first_response) if first_response is not None else None
                ),
                "first_content_at_utc": (
                    _format_utc(first_content_epoch)
                    if first_content_epoch is not None
                    else None
                ),
                "response_finished_at_utc": result["finished_at_utc"],
                "response_elapsed_seconds": round(finished - started, 6),
                "response_start_segment_index": start_segment,
                "response_end_segment_index": end_segment,
                "response_segments_spanned": end_segment - start_segment + 1,
                "finish_reason": result.get("finish_reason"),
                "output_tokens": result.get("output_tokens"),
            }
        )
        prompt_intervals.append((prompt_index, started, finished))

    segment_rows: list[dict[str, Any]] = []
    for zero_index in range(segment_count):
        segment_start = origin + zero_index * segment_seconds
        segment_end = min(origin + (zero_index + 1) * segment_seconds, session_end)
        duration = max(0.0, segment_end - segment_start)
        in_segment = [
            packet
            for packet in packets
            if segment_start <= packet.epoch_seconds < segment_end
        ]
        uplink = [packet for packet in in_segment if packet.direction == "uplink"]
        downlink = [packet for packet in in_segment if packet.direction == "downlink"]
        prompts_started = [
            str(index)
            for index, started, _ in prompt_intervals
            if segment_start <= started < segment_end
        ]
        active_responses = [
            str(index)
            for index, started, finished in prompt_intervals
            if started < segment_end and finished > segment_start
        ]
        segment_rows.append(
            {
                "segment_index": zero_index + 1,
                "segment_start_at_utc": _format_utc(segment_start),
                "segment_end_at_utc": _format_utc(segment_end),
                "segment_start_offset_seconds": round(zero_index * segment_seconds, 6),
                "segment_end_offset_seconds": round(segment_end - origin, 6),
                "segment_duration_seconds": round(duration, 6),
                "uplink_packets": len(uplink),
                "uplink_frame_bytes": sum(packet.frame_bytes for packet in uplink),
                "uplink_payload_bytes": sum(packet.payload_bytes for packet in uplink),
                "downlink_packets": len(downlink),
                "downlink_frame_bytes": sum(packet.frame_bytes for packet in downlink),
                "downlink_payload_bytes": sum(packet.payload_bytes for packet in downlink),
                "uplink_frame_bytes_per_second": (
                    round(sum(packet.frame_bytes for packet in uplink) / duration, 6)
                    if duration
                    else 0.0
                ),
                "downlink_frame_bytes_per_second": (
                    round(sum(packet.frame_bytes for packet in downlink) / duration, 6)
                    if duration
                    else 0.0
                ),
                "prompt_indices_started": ";".join(prompts_started),
                "active_response_prompt_indices": ";".join(active_responses),
            }
        )
    return segment_rows, prompt_rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze_session_timeline(
    results_path: Path,
    capture_path: Path,
    output_dir: Path,
    server_port: int,
    transport: str,
    segment_seconds: float = 30.0,
    tshark: str = "tshark",
) -> tuple[Path, Path]:
    packets = read_packets(capture_path, server_port, transport, tshark=tshark)
    segments, prompts = build_timeline(
        read_jsonl(results_path),
        packets,
        segment_seconds=segment_seconds,
    )
    segments_path = output_dir / "timeline_30s_segments.csv"
    prompts_path = output_dir / "prompt_upload_events.csv"
    _write_csv(segments_path, segments)
    _write_csv(prompts_path, prompts)
    return segments_path, prompts_path
