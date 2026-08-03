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


def summarize_capture(
    capture_path: Path,
    server_port: int,
    transport: str = "http1",
    tshark: str = "tshark",
) -> dict[str, int | str]:
    if shutil.which(tshark) is None:
        raise FileNotFoundError(f"{tshark} was not found in PATH")
    command = [
        tshark,
        "-r",
        str(capture_path),
    ]
    if transport == "http3":
        command.extend(["-d", f"udp.port=={server_port},quic"])
    elif transport == "tls13":
        command.extend(["-d", f"tcp.port=={server_port},tls"])
    command.extend(
        [
        "-Y",
        "tcp || udp",
        "-T",
        "fields",
        "-E",
        "separator=/t",
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
        "-e",
        "udp.srcport",
        "-e",
        "udp.dstport",
        "-e",
        "udp.length",
        "-e",
        "quic.header_form",
        "-e",
        "tls.record.version",
        "-e",
        "tls.handshake.extensions_alpn_str",
        ]
    )
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
        "udp_payload_bytes_client_to_server": 0,
        "udp_payload_bytes_server_to_client": 0,
        "quic_packets": 0,
        "tls_records": 0,
        "observed_tls_versions": "",
        "observed_alpn": "",
    }
    tls_versions: set[str] = set()
    alpn_values: set[str] = set()
    for raw_line in process.stdout.splitlines():
        fields = (raw_line.split("\t") + [""] * 11)[:11]
        (
            frame_len,
            tcp_src,
            tcp_dst,
            tcp_len,
            retransmission,
            udp_src,
            udp_dst,
            udp_len,
            quic_header,
            tls_version,
            alpn,
        ) = fields
        src_port = udp_src if transport == "http3" else tcp_src
        dst_port = udp_dst if transport == "http3" else tcp_dst
        frame_bytes = _integer(frame_len)
        tcp_payload_bytes = _integer(tcp_len)
        udp_payload_bytes = max(0, _integer(udp_len) - 8)
        metrics["packets_total"] += 1
        metrics["bytes_total"] += frame_bytes
        if retransmission:
            metrics["tcp_retransmissions"] += 1
        if quic_header:
            metrics["quic_packets"] += 1
        if tls_version:
            metrics["tls_records"] += 1
            tls_versions.update(item for item in tls_version.split(",") if item)
        if alpn:
            alpn_values.update(item for item in alpn.split(",") if item)
        if _integer(dst_port) == server_port:
            metrics["packets_client_to_server"] += 1
            metrics["bytes_client_to_server"] += frame_bytes
            metrics["tcp_payload_bytes_client_to_server"] += tcp_payload_bytes
            metrics["udp_payload_bytes_client_to_server"] += udp_payload_bytes
        elif _integer(src_port) == server_port:
            metrics["packets_server_to_client"] += 1
            metrics["bytes_server_to_client"] += frame_bytes
            metrics["tcp_payload_bytes_server_to_client"] += tcp_payload_bytes
            metrics["udp_payload_bytes_server_to_client"] += udp_payload_bytes
    metrics["observed_tls_versions"] = ";".join(sorted(tls_versions))
    metrics["observed_alpn"] = ";".join(sorted(alpn_values))
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
                "backend",
                "task_type",
                "transport",
                "connection_mode",
                "negotiated_http_version",
                "completed",
                "http_status",
                "elapsed_seconds",
                "input_tokens",
                "output_tokens",
                "capture_file",
                "error",
            ]
        }
        reference = result.get("reference_answer")
        prediction = result.get("response_text")
        if reference and prediction:
            try:
                from locomo_eval.metrics.answer_metrics import rouge_l, token_f1

                row["token_f1"] = token_f1(str(prediction), str(reference))
                row["rouge_l"] = rouge_l(str(prediction), str(reference))
                row["quality_error"] = None
            except Exception as exc:
                row["quality_error"] = f"{type(exc).__name__}: {exc}"
        else:
            row["quality_error"] = "prediction or reference is absent"
        capture_value = result.get("capture_file")
        if capture_value and Path(capture_value).exists():
            try:
                row.update(
                    summarize_capture(
                        Path(capture_value),
                        int(result["backend_port"]),
                        transport=str(result.get("transport", "http1")),
                        tshark=tshark,
                    )
                )
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
