from __future__ import annotations

import csv
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .common import read_jsonl


def _integer(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize_capture(capture_path: Path, server_port: int, tshark: str = "tshark") -> dict[str, int]:
    if shutil.which(tshark) is None:
        raise FileNotFoundError(f"{tshark} was not found in PATH")
    command = [
        tshark,
        "-r",
        str(capture_path),
        "-Y",
        "tcp",
        "-T",
        "fields",
        "-E",
        "separator=,",
        "-E",
        "quote=n",
        "-e",
        "frame.len",
        "-e",
        "tcp.srcport",
        "-e",
        "tcp.dstport",
        "-e",
        "tcp.len",
        "-e",
        "tcp.analysis.retransmission",
    ]
    process = subprocess.run(command, check=True, capture_output=True, text=True)
    metrics = {
        "packets_total": 0,
        "bytes_total": 0,
        "packets_client_to_server": 0,
        "bytes_client_to_server": 0,
        "tcp_payload_bytes_client_to_server": 0,
        "packets_server_to_client": 0,
        "bytes_server_to_client": 0,
        "tcp_payload_bytes_server_to_client": 0,
        "tcp_retransmissions": 0,
    }
    for raw_line in process.stdout.splitlines():
        fields = (raw_line.split(",") + ["", "", "", "", ""])[:5]
        frame_len, src_port, dst_port, tcp_len, retransmission = fields
        frame_bytes = _integer(frame_len)
        payload_bytes = _integer(tcp_len)
        metrics["packets_total"] += 1
        metrics["bytes_total"] += frame_bytes
        if retransmission:
            metrics["tcp_retransmissions"] += 1
        if _integer(dst_port) == server_port:
            metrics["packets_client_to_server"] += 1
            metrics["bytes_client_to_server"] += frame_bytes
            metrics["tcp_payload_bytes_client_to_server"] += payload_bytes
        elif _integer(src_port) == server_port:
            metrics["packets_server_to_client"] += 1
            metrics["bytes_server_to_client"] += frame_bytes
            metrics["tcp_payload_bytes_server_to_client"] += payload_bytes
    return metrics


def analyze_results(results_path: Path, output_csv: Path, tshark: str = "tshark") -> Path:
    results = read_jsonl(results_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for result in results:
        row = {
            key: result.get(key)
            for key in [
                "run_id",
                "request_id",
                "sample_id",
                "condition",
                "repetition",
                "model",
                "completed",
                "http_status",
                "elapsed_seconds",
                "input_tokens",
                "output_tokens",
                "capture_file",
                "error",
            ]
        }
        capture_value = result.get("capture_file")
        if capture_value and Path(capture_value).exists():
            try:
                row.update(summarize_capture(Path(capture_value), int(result["backend_port"]), tshark=tshark))
                row["analysis_error"] = None
            except Exception as exc:
                row["analysis_error"] = f"{type(exc).__name__}: {exc}"
        else:
            row["analysis_error"] = "capture file is absent"
        rows.append(row)

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output_csv
