from __future__ import annotations

import csv
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "artifacts" / "preliminary_metrics_baseline.csv"
OUTPUT = ROOT / "figures" / "data"
CONDITIONS = [
    "no_compression",
    "longllmlingua_2x",
    "longllmlingua_4x",
]
WORKLOADS = ["qa", "summary"]
TRANSPORTS = ["tls13", "http3"]
NUMERIC = [
    "elapsed_seconds",
    "input_tokens",
    "output_tokens",
    "packets_total",
    "bytes_total",
    "bytes_client_to_server",
    "bytes_server_to_client",
]


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_median_ci(
    values: list[float], seed: int, iterations: int = 4000
) -> tuple[float, float]:
    rng = random.Random(seed)
    medians = [
        statistics.median(rng.choices(values, k=len(values)))
        for _ in range(iterations)
    ]
    return quantile(medians, 0.025), quantile(medians, 0.975)


def write_csv(name: str, rows: list[dict[str, object]]) -> None:
    path = OUTPUT / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


with INPUT.open(encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
for row in rows:
    for key in NUMERIC:
        row[key] = float(row[key])

OUTPUT.mkdir(parents=True, exist_ok=True)
lookup = {
    (row["workload"], row["transport"], row["sample_id"], row["condition"]): row
    for row in rows
}

group_medians: list[dict[str, object]] = []
seed = 42
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        for condition in CONDITIONS:
            group = [
                row
                for row in rows
                if row["workload"] == workload
                and row["transport"] == transport
                and row["condition"] == condition
            ]
            for metric in NUMERIC:
                values = [float(row[metric]) for row in group]
                low, high = bootstrap_median_ci(values, seed)
                seed += 1
                group_medians.append(
                    {
                        "workload": workload,
                        "workload_code": WORKLOADS.index(workload),
                        "transport": transport,
                        "transport_code": TRANSPORTS.index(transport),
                        "condition": condition,
                        "condition_code": CONDITIONS.index(condition),
                        "metric": metric,
                        "n": len(values),
                        "median": statistics.median(values),
                        "q1": quantile(values, 0.25),
                        "q3": quantile(values, 0.75),
                        "ci_low": low,
                        "ci_high": high,
                    }
                )
write_csv("group_medians.csv", group_medians)

paired_rows: list[dict[str, object]] = []
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        samples = sorted(
            {
                row["sample_id"]
                for row in rows
                if row["workload"] == workload and row["transport"] == transport
            }
        )
        for sample_id in samples:
            baseline = lookup[
                (workload, transport, sample_id, "no_compression")
            ]
            for condition in CONDITIONS[1:]:
                compressed = lookup[(workload, transport, sample_id, condition)]
                paired_rows.append(
                    {
                        "workload": workload,
                        "transport": transport,
                        "sample_id": sample_id,
                        "condition": condition,
                        **{
                            f"{metric}_ratio": (
                                float(compressed[metric]) / float(baseline[metric])
                            )
                            for metric in NUMERIC
                        },
                    }
                )
write_csv("paired_ratios.csv", paired_rows)

paired_summaries: list[dict[str, object]] = []
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        for condition in CONDITIONS[1:]:
            group = [
                row
                for row in paired_rows
                if row["workload"] == workload
                and row["transport"] == transport
                and row["condition"] == condition
            ]
            for metric in [
                "input_tokens_ratio",
                "bytes_client_to_server_ratio",
                "bytes_total_ratio",
                "packets_total_ratio",
                "elapsed_seconds_ratio",
            ]:
                values = [float(row[metric]) for row in group]
                low, high = bootstrap_median_ci(values, seed)
                seed += 1
                paired_summaries.append(
                    {
                        "workload": workload,
                        "transport": transport,
                        "condition": condition,
                        "metric": metric,
                        "n": len(values),
                        "median": statistics.median(values),
                        "ci_low": low,
                        "ci_high": high,
                    }
                )
write_csv("paired_ratio_summaries.csv", paired_summaries)

protocol_rows: list[dict[str, object]] = []
for workload in WORKLOADS:
    samples = sorted({row["sample_id"] for row in rows if row["workload"] == workload})
    for sample_id in samples:
        for condition in CONDITIONS:
            tls = lookup[(workload, "tls13", sample_id, condition)]
            quic = lookup[(workload, "http3", sample_id, condition)]
            protocol_rows.append(
                {
                    "workload": workload,
                    "sample_id": sample_id,
                    "condition": condition,
                    **{
                        f"{metric}_ratio": float(quic[metric]) / float(tls[metric])
                        for metric in NUMERIC
                    },
                }
            )
write_csv("protocol_ratios.csv", protocol_rows)

protocol_summaries: list[dict[str, object]] = []
for workload in WORKLOADS:
    for condition in CONDITIONS:
        group = [
            row
            for row in protocol_rows
            if row["workload"] == workload and row["condition"] == condition
        ]
        for metric in [
            "bytes_client_to_server_ratio",
            "bytes_server_to_client_ratio",
            "bytes_total_ratio",
            "packets_total_ratio",
            "elapsed_seconds_ratio",
        ]:
            values = [float(row[metric]) for row in group]
            low, high = bootstrap_median_ci(values, seed)
            seed += 1
            protocol_summaries.append(
                {
                    "workload": workload,
                    "condition": condition,
                    "metric": metric,
                    "n": len(values),
                    "median": statistics.median(values),
                    "ci_low": low,
                    "ci_high": high,
                }
            )
write_csv("protocol_ratio_summaries.csv", protocol_summaries)

scatter_rows = [
    {
        "workload": row["workload"],
        "workload_code": WORKLOADS.index(row["workload"]),
        "transport": row["transport"],
        "transport_code": TRANSPORTS.index(row["transport"]),
        "condition": row["condition"],
        "condition_code": CONDITIONS.index(row["condition"]),
        "sample_id": row["sample_id"],
        "input_tokens": row["input_tokens"],
        "upload_kib": row["bytes_client_to_server"] / 1024,
        "download_kib": row["bytes_server_to_client"] / 1024,
        "total_kib": row["bytes_total"] / 1024,
        "latency_seconds": row["elapsed_seconds"],
    }
    for row in rows
]
write_csv("scatter.csv", scatter_rows)
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        write_csv(
            f"scatter_{workload}_{transport}.csv",
            [
                row
                for row in scatter_rows
                if row["workload"] == workload and row["transport"] == transport
            ],
        )

direction_rows: list[dict[str, object]] = []
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        for condition in CONDITIONS:
            group = [
                row
                for row in rows
                if row["workload"] == workload
                and row["transport"] == transport
                and row["condition"] == condition
            ]
            up = statistics.median(
                float(row["bytes_client_to_server"]) / 1024 for row in group
            )
            down = statistics.median(
                float(row["bytes_server_to_client"]) / 1024 for row in group
            )
            direction_rows.append(
                {
                    "workload": workload,
                    "transport": transport,
                    "condition": condition,
                    "upload_kib": up,
                    "download_kib": down,
                    "output_share": down / (up + down),
                }
            )
write_csv("direction_medians.csv", direction_rows)

ecdf_rows: list[dict[str, object]] = []
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        for condition in CONDITIONS[1:]:
            values = sorted(
                float(row["bytes_total_ratio"])
                for row in paired_rows
                if row["workload"] == workload
                and row["transport"] == transport
                and row["condition"] == condition
            )
            for index, value in enumerate(values, start=1):
                ecdf_rows.append(
                    {
                        "workload": workload,
                        "transport": transport,
                        "condition": condition,
                        "ratio": value,
                        "ecdf": index / len(values),
                    }
                )
write_csv("total_bytes_ecdf.csv", ecdf_rows)
for workload in WORKLOADS:
    for transport in TRANSPORTS:
        for condition in CONDITIONS[1:]:
            write_csv(
                f"ecdf_{workload}_{transport}_{condition}.csv",
                [
                    row
                    for row in ecdf_rows
                    if row["workload"] == workload
                    and row["transport"] == transport
                    and row["condition"] == condition
                ],
            )


def group_value(
    workload: str, transport: str, condition: str, metric: str
) -> dict[str, object]:
    return next(
        row
        for row in group_medians
        if row["workload"] == workload
        and row["transport"] == transport
        and row["condition"] == condition
        and row["metric"] == metric
    )


def paired_value(
    workload: str, transport: str, condition: str, metric: str
) -> dict[str, object]:
    return next(
        row
        for row in paired_summaries
        if row["workload"] == workload
        and row["transport"] == transport
        and row["condition"] == condition
        and row["metric"] == metric
    )


def protocol_value(
    workload: str, condition: str, metric: str
) -> dict[str, object]:
    return next(
        row
        for row in protocol_summaries
        if row["workload"] == workload
        and row["condition"] == condition
        and row["metric"] == metric
    )


condition_labels = {
    "no_compression": "Original",
    "longllmlingua_2x": "2x",
    "longllmlingua_4x": "4x",
}
token_plot = []
for index, condition in enumerate(CONDITIONS):
    token_plot.append(
        {
            "x": index,
            "label": condition_labels[condition],
            "qa_k": float(
                group_value("qa", "tls13", condition, "input_tokens")["median"]
            )
            / 1000,
            "summary_k": float(
                group_value("summary", "tls13", condition, "input_tokens")["median"]
            )
            / 1000,
        }
    )
write_csv("plot_token_medians.csv", token_plot)

for output_name, metric in [
    ("plot_upload_reduction.csv", "bytes_client_to_server_ratio"),
    ("plot_total_reduction.csv", "bytes_total_ratio"),
    ("plot_latency_reduction.csv", "elapsed_seconds_ratio"),
]:
    plot_rows = []
    for index, condition in enumerate(CONDITIONS[1:]):
        row: dict[str, object] = {
            "x": index,
            "label": condition_labels[condition],
        }
        for workload in WORKLOADS:
            for transport in TRANSPORTS:
                value = paired_value(workload, transport, condition, metric)
                prefix = f"{workload}_{transport}"
                median = float(value["median"])
                low = float(value["ci_low"])
                high = float(value["ci_high"])
                row[prefix] = 100 * (1 - median)
                row[f"{prefix}_err_minus"] = 100 * (high - median)
                row[f"{prefix}_err_plus"] = 100 * (median - low)
        plot_rows.append(row)
    write_csv(output_name, plot_rows)

for workload in WORKLOADS:
    plot_rows = []
    for transport_index, transport in enumerate(TRANSPORTS):
        for condition_index, condition in enumerate(CONDITIONS):
            direction = next(
                row
                for row in direction_rows
                if row["workload"] == workload
                and row["transport"] == transport
                and row["condition"] == condition
            )
            plot_rows.append(
                {
                    "x": transport_index * 4 + condition_index,
                    "label": (
                        ("TLS " if transport == "tls13" else "H3 ")
                        + condition_labels[condition]
                    ),
                    "upload_kib": direction["upload_kib"],
                    "download_kib": direction["download_kib"],
                    "output_share_pct": 100 * float(direction["output_share"]),
                }
            )
    write_csv(f"plot_direction_{workload}.csv", plot_rows)

protocol_plot = []
for index, condition in enumerate(CONDITIONS):
    row = {"x": index, "label": condition_labels[condition]}
    for workload in WORKLOADS:
        for metric, short_name in [
            ("bytes_total_ratio", "bytes"),
            ("packets_total_ratio", "packets"),
        ]:
            value = protocol_value(workload, condition, metric)
            row[f"{workload}_{short_name}"] = float(value["median"])
    protocol_plot.append(row)
write_csv("plot_protocol_ratio.csv", protocol_plot)

latency_plot = []
for index, condition in enumerate(CONDITIONS):
    row = {"x": index, "label": condition_labels[condition]}
    for workload in WORKLOADS:
        for transport in TRANSPORTS:
            row[f"{workload}_{transport}"] = float(
                group_value(
                    workload, transport, condition, "elapsed_seconds"
                )["median"]
            )
    latency_plot.append(row)
write_csv("plot_latency_medians.csv", latency_plot)

total_plot = []
for index, condition in enumerate(CONDITIONS):
    row = {"x": index, "label": condition_labels[condition]}
    for workload in WORKLOADS:
        for transport in TRANSPORTS:
            row[f"{workload}_{transport}"] = float(
                group_value(workload, transport, condition, "bytes_total")["median"]
            ) / 1024
    total_plot.append(row)
write_csv("plot_total_kib.csv", total_plot)


def median_ratio(
    workload: str, transport: str, condition: str, metric: str
) -> float:
    return statistics.median(
        float(row[metric])
        for row in paired_rows
        if row["workload"] == workload
        and row["transport"] == transport
        and row["condition"] == condition
    )


def pearson(xs: list[float], ys: list[float]) -> float:
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x in xs)
        * sum((y - mean_y) ** 2 for y in ys)
    )
    return numerator / denominator


correlations = {}
for transport in TRANSPORTS:
    group = [row for row in rows if row["transport"] == transport]
    correlations[transport] = pearson(
        [float(row["input_tokens"]) for row in group],
        [float(row["bytes_client_to_server"]) for row in group],
    )

macros = {
    "NumCaptures": "144",
    "NumQASamples": "16",
    "NumSummarySamples": "8",
    "QATwoUploadReductionTLS": f"{100 * (1 - median_ratio('qa', 'tls13', 'longllmlingua_2x', 'bytes_client_to_server_ratio')):.0f}",
    "QAFourUploadReductionTLS": f"{100 * (1 - median_ratio('qa', 'tls13', 'longllmlingua_4x', 'bytes_client_to_server_ratio')):.0f}",
    "QATwoTotalReductionTLS": f"{100 * (1 - median_ratio('qa', 'tls13', 'longllmlingua_2x', 'bytes_total_ratio')):.0f}",
    "QAFourTotalReductionTLS": f"{100 * (1 - median_ratio('qa', 'tls13', 'longllmlingua_4x', 'bytes_total_ratio')):.0f}",
    "SummaryTwoUploadReductionTLS": f"{100 * (1 - median_ratio('summary', 'tls13', 'longllmlingua_2x', 'bytes_client_to_server_ratio')):.0f}",
    "SummaryFourUploadReductionTLS": f"{100 * (1 - median_ratio('summary', 'tls13', 'longllmlingua_4x', 'bytes_client_to_server_ratio')):.0f}",
    "SummaryTwoTotalReductionTLS": f"{100 * (1 - median_ratio('summary', 'tls13', 'longllmlingua_2x', 'bytes_total_ratio')):.0f}",
    "SummaryFourTotalReductionTLS": f"{100 * (1 - median_ratio('summary', 'tls13', 'longllmlingua_4x', 'bytes_total_ratio')):.0f}",
    "TokenUploadCorrelationTLS": f"{correlations['tls13']:.2f}",
    "TokenUploadCorrelationQUIC": f"{correlations['http3']:.2f}",
}
with (OUTPUT / "findings.tex").open("w", encoding="utf-8") as handle:
    for name, value in macros.items():
        handle.write(f"\\newcommand{{\\{name}}}{{{value}}}\n")

print(f"Prepared figure data in {OUTPUT}")
