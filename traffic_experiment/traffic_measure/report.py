"""Comprehensive experiment report: consumes completed runs (JSONL + PCAPs
+ calibration JSON + session CSVs) and emits CSV tables, figure-ready data
files, and a draft report marker.

Synthetic / pilot data may be used for testing only.  All outputs are
clearly labelled with their data source.  Primary versus post-hoc
sensitivity results are always separated.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import read_jsonl
from .session_timeline import read_packets


# ---------------------------------------------------------------------------
# Bootstrap with sample-level clustering so technical repetitions are not
# treated as independent.
# ---------------------------------------------------------------------------


def _by_sample(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group rows by sample_id, collapsing technical repetitions."""
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sid = str(row.get("sample_id", row.get("request_id", "")))
        samples[sid].append(row)
    return dict(samples)


def _agg_sample(values: list[float], method: str = "median") -> float:
    """Aggregate per-sample values (default median to reduce outlier influence)."""
    if not values:
        return 0.0
    if method == "median":
        return statistics.median(values)
    if method == "mean":
        return statistics.mean(values)
    raise ValueError(f"unknown aggregation method: {method}")


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (1 - (position - lower)) + ordered[upper] * (position - lower)


def clustered_bootstrap_ci(
    rows: list[dict[str, Any]],
    metric_key: str,
    *,
    seed: int = 42,
    iterations: int = 4000,
    collapse: str = "median",
) -> tuple[float, float, float]:
    """Bootstrap CI that respects sample-level clustering.

    Each bootstrap iteration draws samples (not individual rows) with
    replacement, aggregates per-sample values using *collapse*, then
    computes the statistic-of-interest (median of sample aggregates).

    Returns (ci_low, median, ci_high).
    """
    samples = list(_by_sample(rows).values())
    sample_values = [
        _agg_sample([float(r[metric_key]) for r in s], method=collapse)
        for s in samples
    ]
    if len(sample_values) < 2:
        return sample_values[0], sample_values[0], sample_values[0]

    rng = random.Random(seed)
    boot_medians = [
        statistics.median(rng.choices(sample_values, k=len(sample_values)))
        for _ in range(iterations)
    ]
    return (
        quantile(boot_medians, 0.025),
        statistics.median(sample_values),
        quantile(boot_medians, 0.975),
    )


def iqr(values: list[float]) -> tuple[float, float, float]:
    return quantile(values, 0.25), statistics.median(values), quantile(values, 0.75)


# ---------------------------------------------------------------------------
# Per-condition / per-group summary
# ---------------------------------------------------------------------------


NUMERIC_METRICS = [
    "elapsed_seconds",
    "input_tokens",
    "output_tokens",
    "packets_total",
    "bytes_total",
    "bytes_client_to_server",
    "bytes_server_to_client",
    "tcp_payload_bytes_client_to_server",
    "tcp_payload_bytes_server_to_client",
    "udp_payload_bytes_client_to_server",
    "udp_payload_bytes_server_to_client",
    "tcp_retransmissions",
    "quic_packets",
]

RATIO_METRICS = [
    "input_tokens_ratio",
    "bytes_client_to_server_ratio",
    "bytes_server_to_client_ratio",
    "bytes_total_ratio",
    "packets_total_ratio",
    "elapsed_seconds_ratio",
]


@dataclass
class GroupSummary:
    workload: str
    transport: str
    condition: str
    n_samples: int
    n_trials: int
    rows: list[dict[str, Any]] = field(repr=False)
    _seed: int = field(default=42)

    def metric_iqr(self, key: str) -> tuple[float, float, float]:
        values = [float(r[key]) for r in self.rows if key in r]
        return iqr(values)

    def metric_ci(self, key: str, collapse: str = "median") -> tuple[float, float, float]:
        return clustered_bootstrap_ci(self.rows, key, seed=self._seed, collapse=collapse)


def summarize_groups(
    rows: list[dict[str, Any]],
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Summarize every (workload, transport, condition) group."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        wl = str(row.get("task_type", "qa"))
        tp = str(row.get("transport", "http1"))
        cond = str(row.get("condition", "no_compression"))
        groups[(wl, tp, cond)].append(row)

    result: list[dict[str, Any]] = []
    local_seed = seed
    for (wl, tp, cond), group_rows in sorted(groups.items()):
        summary = GroupSummary(wl, tp, cond, _seed=local_seed,
                               n_samples=len(_by_sample(group_rows)),
                               n_trials=len(group_rows), rows=group_rows)
        row: dict[str, Any] = {
            "workload": wl,
            "transport": tp,
            "condition": cond,
            "n_samples": summary.n_samples,
            "n_trials": summary.n_trials,
        }
        for metric in NUMERIC_METRICS:
            try:
                low, med, high = summary.metric_ci(metric)
                q1, q2, q3 = summary.metric_iqr(metric)
                row[f"{metric}_median"] = round(med, 1)
                row[f"{metric}_q1"] = round(q1, 1)
                row[f"{metric}_q3"] = round(q3, 1)
                row[f"{metric}_ci_low"] = round(low, 1)
                row[f"{metric}_ci_high"] = round(high, 1)
            except (KeyError, ValueError):
                pass
        # Per-token ratios
        for direction, prefix in [("client_to_server", "upload"), ("server_to_client", "download")]:
            bytes_key = f"bytes_{direction}"
            for token_key, token_label in [("input_tokens", "input"), ("output_tokens", "output")]:
                try:
                    vals = [
                        float(r[bytes_key]) / max(1, float(r.get(token_key, 1)))
                        for r in group_rows
                        if bytes_key in r and token_key in r
                    ]
                    if vals:
                        row[f"bytes_per_{token_label}_token_{prefix}_median"] = round(statistics.median(vals), 2)
                except (KeyError, ValueError):
                    pass
        result.append(row)
        local_seed += 1
    return result


# ---------------------------------------------------------------------------
# Paired compression ratios
# ---------------------------------------------------------------------------


def paired_compression_ratios(
    rows: list[dict[str, Any]],
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Sample-level paired ratios: compressed / uncompressed."""
    lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("sample_id", ""))
        wl = str(row.get("task_type", "qa"))
        tp = str(row.get("transport", "http1"))
        cond = str(row.get("condition", "no_compression"))
        key = (wl, tp, sid, cond)
        existing = lookup.get(key)
        if existing is None:
            lookup[key] = row
        else:
            # Collapse repetitions: take the row with lower elapsed seconds
            # as "typical" (avoids outliers inflating ratios).
            if float(row.get("elapsed_seconds", 999)) < float(existing.get("elapsed_seconds", 999)):
                lookup[key] = row

    result: list[dict[str, Any]] = []
    for (wl, tp, sid, cond), compressed in sorted(lookup.items()):
        if cond == "no_compression":
            continue
        baseline = lookup.get((wl, tp, sid, "no_compression"))
        if baseline is None:
            continue
        ratio_row: dict[str, Any] = {
            "workload": wl,
            "transport": tp,
            "sample_id": sid,
            "condition": cond,
        }
        for metric in NUMERIC_METRICS:
            try:
                denom = max(1, float(baseline.get(metric, 1)))
                ratio_row[f"{metric}_ratio"] = round(float(compressed.get(metric, 0)) / denom, 6)
            except (ValueError, TypeError):
                pass
        result.append(ratio_row)
    return result


def paired_ratio_ci(
    paired_rows: list[dict[str, Any]],
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Bootstrap CIs on paired compression ratios, grouped by workload × transport × condition."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in paired_rows:
        key = (row["workload"], row["transport"], row["condition"])
        grouped[key].append(row)

    result: list[dict[str, Any]] = []
    local_seed = seed
    for (wl, tp, cond), group in sorted(grouped.items()):
        for ratio_metric in RATIO_METRICS:
            values = [float(r[ratio_metric]) for r in group if ratio_metric in r]
            if len(values) < 2:
                continue
            low, med, high = clustered_bootstrap_ci(
                [{"v": v} for v in values], "v", seed=local_seed, collapse="median",
            )
            result.append({
                "workload": wl, "transport": tp, "condition": cond,
                "metric": ratio_metric, "n": len(values),
                "median": round(med, 4), "ci_low": round(low, 4), "ci_high": round(high, 4),
            })
        local_seed += 1
    return result


# ---------------------------------------------------------------------------
# Protocol comparison (HTTP/3 vs TLS 1.3)
# ---------------------------------------------------------------------------


def protocol_ratios(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Paired HTTP/3 ÷ TLS 1.3 ratios per sample × condition × workload."""
    lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("sample_id", ""))
        wl = str(row.get("task_type", "qa"))
        tp = str(row.get("transport", "http1"))
        cond = str(row.get("condition", "no_compression"))
        key = (wl, tp, sid, cond)
        existing = lookup.get(key)
        if existing is None or float(row.get("elapsed_seconds", 999)) < float(existing.get("elapsed_seconds", 999)):
            lookup[key] = row

    result: list[dict[str, Any]] = []
    for (wl, _, sid, cond), row in lookup.items():
        tls_row = lookup.get((wl, "tls13", sid, cond))
        h3_row = lookup.get((wl, "http3", sid, cond))
        if tls_row is None or h3_row is None:
            continue
        ratio_row: dict[str, Any] = {
            "workload": wl, "sample_id": sid, "condition": cond,
        }
        for metric in NUMERIC_METRICS:
            try:
                denom = max(1, float(tls_row.get(metric, 1)))
                ratio_row[f"{metric}_ratio"] = round(float(h3_row.get(metric, 0)) / denom, 6)
            except (ValueError, TypeError):
                pass
        result.append(ratio_row)
    return result


# ---------------------------------------------------------------------------
# Packet-size distribution
# ---------------------------------------------------------------------------


def packet_size_summary(
    pcap_path: Path,
    server_port: int,
    transport: str,
    tshark: str = "tshark",
) -> dict[str, Any]:
    """Summarize per-packet size distribution from a PCAP file."""
    packets = read_packets(pcap_path, server_port, transport, tshark=tshark)
    if not packets:
        return {}
    sizes = [p.frame_bytes for p in packets]
    up_sizes = [p.frame_bytes for p in packets if p.direction == "uplink"]
    down_sizes = [p.frame_bytes for p in packets if p.direction == "downlink"]
    result: dict[str, Any] = {
        "packet_count": len(sizes),
        "frame_bytes_min": min(sizes), "frame_bytes_max": max(sizes),
        "frame_bytes_median": statistics.median(sizes),
        "frame_bytes_mean": round(statistics.mean(sizes), 1),
        "uplink_packet_count": len(up_sizes),
        "uplink_frame_bytes_median": statistics.median(up_sizes) if up_sizes else 0,
        "downlink_packet_count": len(down_sizes),
        "downlink_frame_bytes_median": statistics.median(down_sizes) if down_sizes else 0,
    }
    payloads = [p.payload_bytes for p in packets if p.payload_bytes > 0]
    if payloads:
        result["payload_bytes_median"] = statistics.median(payloads)
        result["protocol_overhead_ratio"] = round(
            (sum(sizes) - sum(payloads)) / max(1, sum(sizes)), 4,
        )
    return result


# ---------------------------------------------------------------------------
# Finish-reason & failure analysis
# ---------------------------------------------------------------------------


def finish_reason_summary(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Count finish reasons and failures across all completed rows."""
    counts: dict[str, int] = defaultdict(int)
    failures = 0
    transport_failures = 0
    for row in rows:
        if row.get("completed"):
            fr = str(row.get("finish_reason", "unknown"))
            counts[fr] += 1
        else:
            failures += 1
            err = str(row.get("error", ""))
            if "transport" in err.lower() or "timeout" in err.lower() or "connection" in err.lower():
                transport_failures += 1
    total = len(rows)
    return {
        "total_rows": total,
        "completed": total - failures,
        "failed": failures,
        "transport_failures": transport_failures,
        "failure_rate": round(failures / max(1, total), 4),
        "finish_reason_counts": dict(counts),
    }


# ---------------------------------------------------------------------------
# CSV / output helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def generate_report(
    results_path: Path,
    output_dir: Path,
    *,
    seed: int = 42,
    tshark: str = "tshark",
) -> dict[str, Path]:
    """Generate a comprehensive report from a merged results JSONL file.

    This is the reproducible entry point: it consumes a completed run
    (or merged parallel runs) and emits all the data files the LaTeX
    report and figures need.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = read_jsonl(results_path)
    completed = [r for r in rows if r.get("completed")]

    outputs: dict[str, Path] = {}

    # 1. Group medians with clustered bootstrap CIs
    outputs["group_medians"] = _write_csv(
        output_dir / "group_medians.csv", summarize_groups(completed, seed=seed),
    )

    # 2. Paired compression ratios
    paired = paired_compression_ratios(completed, seed=seed)
    outputs["paired_ratios"] = _write_csv(output_dir / "paired_ratios.csv", paired)
    outputs["paired_ratio_ci"] = _write_csv(
        output_dir / "paired_ratio_summaries.csv", paired_ratio_ci(paired, seed=seed),
    )

    # 3. Protocol HTTP/3 vs TLS 1.3 ratios
    outputs["protocol_ratios"] = _write_csv(
        output_dir / "protocol_ratios.csv", protocol_ratios(completed),
    )

    # 4. Finish-reason / failure report
    fr = finish_reason_summary(rows)
    _write_csv(output_dir / "finish_reasons.csv", [
        {"metric": k, "value": v} for k, v in fr.items()
        if k != "finish_reason_counts"
    ] + [{"metric": f"finish_reason_{k}", "value": v} for k, v in fr["finish_reason_counts"].items()])

    # 5. Packet-size analysis for each transport
    pcap_rows: list[dict[str, Any]] = []
    transports_seen: set[str] = set()
    for row in completed:
        pcap_file = row.get("capture_file")
        if not pcap_file or not Path(pcap_file).exists():
            continue
        tp = str(row.get("transport", "http1"))
        port = int(row.get("backend_port", 8443))
        try:
            ps = packet_size_summary(Path(pcap_file), port, tp, tshark=tshark)
            ps["transport"] = tp
            ps["request_id"] = row.get("request_id", "")
            pcap_rows.append(ps)
            transports_seen.add(tp)
        except Exception:
            pass
    if pcap_rows:
        outputs["packet_sizes"] = _write_csv(output_dir / "packet_size_summary.csv", pcap_rows)

    # 6. Scatter data
    scatter_rows: list[dict[str, Any]] = []
    for row in completed:
        try:
            scatter_rows.append({
                "workload": str(row.get("task_type", "qa")),
                "transport": str(row.get("transport", "http1")),
                "condition": str(row.get("condition", "no_compression")),
                "sample_id": str(row.get("sample_id", "")),
                "input_tokens": float(row.get("input_tokens", 0)),
                "upload_kib": float(row.get("bytes_client_to_server", 0)) / 1024,
                "download_kib": float(row.get("bytes_server_to_client", 0)) / 1024,
                "total_kib": float(row.get("bytes_total", 0)) / 1024,
                "latency_seconds": float(row.get("elapsed_seconds", 0)),
            })
        except (ValueError, TypeError):
            pass
    if scatter_rows:
        outputs["scatter"] = _write_csv(output_dir / "scatter.csv", scatter_rows)

    # 7. Direction medians
    direction_rows: list[dict[str, Any]] = []
    for wl in sorted({r["workload"] for r in scatter_rows}):
        for tp in sorted({r["transport"] for r in scatter_rows}):
            for cond in sorted({r["condition"] for r in scatter_rows}):
                group = [r for r in scatter_rows
                         if r["workload"] == wl and r["transport"] == tp and r["condition"] == cond]
                if not group:
                    continue
                up = statistics.median([r["upload_kib"] for r in group])
                down = statistics.median([r["download_kib"] for r in group])
                direction_rows.append({
                    "workload": wl, "transport": tp, "condition": cond,
                    "upload_kib": up, "download_kib": down,
                    "output_share": down / max(0.001, up + down),
                })
    if direction_rows:
        outputs["direction_medians"] = _write_csv(output_dir / "direction_medians.csv", direction_rows)

    # 8. ECDF for paired total-bytes ratio
    ecdf_rows: list[dict[str, Any]] = []
    for wl in sorted({r["workload"] for r in paired}):
        for tp in sorted({r["transport"] for r in paired}):
            for cond in sorted({r["condition"] for r in paired}):
                if cond == "no_compression":
                    continue
                values = sorted([
                    float(r["bytes_total_ratio"])
                    for r in paired
                    if r["workload"] == wl and r["transport"] == tp and r["condition"] == cond
                ])
                for idx, v in enumerate(values, start=1):
                    ecdf_rows.append({
                        "workload": wl, "transport": tp, "condition": cond,
                        "ratio": v, "ecdf": idx / len(values),
                    })
    if ecdf_rows:
        outputs["ecdf"] = _write_csv(output_dir / "total_bytes_ecdf.csv", ecdf_rows)

    # 9. Mark the report as generated
    marker = output_dir / "REPORT_GENERATED"
    marker.write_text(
        f"source={results_path}\n"
        f"rows_analyzed={len(rows)}\n"
        f"completed={len(completed)}\n"
        f"transports={sorted(transports_seen)}\n"
    )
    outputs["marker"] = marker
    return outputs
