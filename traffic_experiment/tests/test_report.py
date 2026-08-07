"""Tests for the comprehensive report module using synthetic fixtures only."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from traffic_experiment.traffic_measure.report import (
    clustered_bootstrap_ci,
    finish_reason_summary,
    generate_report,
    paired_compression_ratios,
    paired_ratio_ci,
    protocol_ratios,
    summarize_groups,
)


# ---------------------------------------------------------------------------
# Synthetic fixture: a minimal 3-sample × 2-condition × 2-rep dataset
# ---------------------------------------------------------------------------

SYNTHETIC_ROWS = [
    # QA, TLS, no_compression, sample A, rep 1+2
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "no_compression",
     "sample_id": "sa", "request_id": "sa-0", "elapsed_seconds": 10.1, "input_tokens": 5000,
     "output_tokens": 200, "bytes_total": 12000, "bytes_client_to_server": 8000,
     "bytes_server_to_client": 4000, "packets_total": 80, "finish_reason": "stop"},
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "no_compression",
     "sample_id": "sa", "request_id": "sa-0-r2", "elapsed_seconds": 10.3, "input_tokens": 5000,
     "output_tokens": 198, "bytes_total": 12100, "bytes_client_to_server": 8100,
     "bytes_server_to_client": 4000, "packets_total": 81, "finish_reason": "stop"},
    # QA, TLS, 2x, sample A, rep 1+2
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "longllmlingua_2x",
     "sample_id": "sa", "request_id": "sa-1", "elapsed_seconds": 6.2, "input_tokens": 2500,
     "output_tokens": 195, "bytes_total": 6500, "bytes_client_to_server": 4200,
     "bytes_server_to_client": 2300, "packets_total": 45, "finish_reason": "stop"},
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "longllmlingua_2x",
     "sample_id": "sa", "request_id": "sa-1-r2", "elapsed_seconds": 6.0, "input_tokens": 2500,
     "output_tokens": 200, "bytes_total": 6400, "bytes_client_to_server": 4150,
     "bytes_server_to_client": 2250, "packets_total": 44, "finish_reason": "stop"},
    # QA, TLS, no_compression, sample B, rep 1+2
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "no_compression",
     "sample_id": "sb", "request_id": "sb-0", "elapsed_seconds": 12.0, "input_tokens": 7000,
     "output_tokens": 250, "bytes_total": 16000, "bytes_client_to_server": 11000,
     "bytes_server_to_client": 5000, "packets_total": 100, "finish_reason": "stop"},
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "no_compression",
     "sample_id": "sb", "request_id": "sb-0-r2", "elapsed_seconds": 12.5, "input_tokens": 7000,
     "output_tokens": 248, "bytes_total": 16200, "bytes_client_to_server": 11100,
     "bytes_server_to_client": 5100, "packets_total": 102, "finish_reason": "stop"},
    # QA, TLS, 2x, sample B, rep 1+2
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "longllmlingua_2x",
     "sample_id": "sb", "request_id": "sb-1", "elapsed_seconds": 7.5, "input_tokens": 3500,
     "output_tokens": 242, "bytes_total": 8500, "bytes_client_to_server": 5500,
     "bytes_server_to_client": 3000, "packets_total": 58, "finish_reason": "stop"},
    {"completed": True, "task_type": "qa", "transport": "tls13", "condition": "longllmlingua_2x",
     "sample_id": "sb", "request_id": "sb-1-r2", "elapsed_seconds": 7.8, "input_tokens": 3500,
     "output_tokens": 245, "bytes_total": 8600, "bytes_client_to_server": 5600,
     "bytes_server_to_client": 3000, "packets_total": 59, "finish_reason": "stop"},
    # A failed row
    {"completed": False, "task_type": "qa", "transport": "tls13", "condition": "no_compression",
     "sample_id": "sc", "request_id": "sc-fail", "elapsed_seconds": 0, "error": "timeout",
     "finish_reason": None},
    # QA, HTTP3, no_compression, sample A (for protocol comparison)
    {"completed": True, "task_type": "qa", "transport": "http3", "condition": "no_compression",
     "sample_id": "sa", "request_id": "sa-h3", "elapsed_seconds": 9.5, "input_tokens": 5000,
     "output_tokens": 205, "bytes_total": 10000, "bytes_client_to_server": 6500,
     "bytes_server_to_client": 3500, "packets_total": 70, "finish_reason": "stop"},
    {"completed": True, "task_type": "qa", "transport": "http3", "condition": "longllmlingua_2x",
     "sample_id": "sa", "request_id": "sa-h3-2x", "elapsed_seconds": 5.8, "input_tokens": 2500,
     "output_tokens": 198, "bytes_total": 5200, "bytes_client_to_server": 3200,
     "bytes_server_to_client": 2000, "packets_total": 38, "finish_reason": "stop"},
]


def _with_capture_provenance(
    tmp_path: Path,
    row: dict,
    index: int,
) -> dict:
    prepared = dict(row)
    if not prepared.get("completed"):
        return prepared
    capture = tmp_path / f"capture-{index}.pcapng"
    capture.write_bytes(f"capture-{index}".encode())
    transport = str(prepared.get("transport", "http1"))
    prepared.update(
        {
            "capture_file": str(capture),
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "capture_return_code": 0,
            "capture_may_be_truncated": False,
            "backend_port": 8444 if transport == "http3" else 8443,
            "negotiated_http_version": (
                "3" if transport == "http3" else "HTTP/1.1"
            ),
            "tcp_packets": 0 if transport == "http3" else 4,
            "udp_packets": 4 if transport == "http3" else 0,
            "quic_packets": 4 if transport == "http3" else 0,
            "tls_records": 0 if transport == "http3" else 4,
        }
    )
    return prepared


class TestClusteredBootstrap:
    def test_respects_sample_clustering(self):
        """Technical repetitions are aggregated per sample before bootstrap."""
        rows = [
            {"sample_id": "s1", "v": 100},
            {"sample_id": "s1", "v": 102},  # rep 2 of s1
            {"sample_id": "s2", "v": 200},
            {"sample_id": "s2", "v": 198},  # rep 2 of s2
        ]
        low, med, high = clustered_bootstrap_ci(rows, "v", seed=42, iterations=2000)
        # Median of per-sample medians: median(101, 199) ≈ 150
        assert 140 < med < 160
        assert low < med < high

    def test_single_sample(self):
        rows = [{"sample_id": "s1", "v": 42}, {"sample_id": "s1", "v": 44}]
        low, med, high = clustered_bootstrap_ci(rows, "v", seed=42)
        assert low == med == high == 43


class TestGroupSummaries:
    def test_summarize_groups(self):
        completed = [r for r in SYNTHETIC_ROWS if r.get("completed")]
        result = summarize_groups(completed, seed=42)
        assert len(result) > 0
        for row in result:
            assert "workload" in row
            assert "transport" in row
            assert "condition" in row
            assert row["n_samples"] > 0
            assert row["n_trials"] >= row["n_samples"]
            assert "bytes_total_median" in row

    def test_bytes_per_token_computed(self):
        completed = [r for r in SYNTHETIC_ROWS if r.get("completed")]
        result = summarize_groups(completed, seed=42)
        tls_no = [r for r in result if r["transport"] == "tls13" and r["condition"] == "no_compression"]
        assert len(tls_no) > 0
        assert "bytes_per_input_token_upload_median" in tls_no[0]

    def test_iqr_collapses_repetitions_at_sample_level(self):
        rows = [
            {"sample_id": "a", "elapsed_seconds": 0.0},
            {"sample_id": "a", "elapsed_seconds": 100.0},
            {"sample_id": "b", "elapsed_seconds": 10.0},
        ]
        summary = summarize_groups(rows, seed=42)[0]
        assert summary["elapsed_seconds_median"] == 30.0
        assert summary["elapsed_seconds_q1"] == 20.0
        assert summary["elapsed_seconds_q3"] == 40.0

    def test_optional_none_metrics_do_not_drop_valid_group_values(self):
        rows = [
            {"sample_id": "a", "input_tokens": None},
            {"sample_id": "b", "input_tokens": 20},
        ]
        summary = summarize_groups(rows, seed=42)[0]
        assert summary["input_tokens_median"] == 20.0


class TestPairedCompression:
    def test_paired_ratios(self):
        paired = paired_compression_ratios(SYNTHETIC_ROWS)
        assert len(paired) > 0
        for row in paired:
            assert row["condition"] != "no_compression"
            assert "bytes_total_ratio" in row
            # 2x compression should reduce bytes
            assert 0 < row["bytes_total_ratio"] < 1.0

    def test_ci_on_paired_ratios(self):
        paired = paired_compression_ratios(SYNTHETIC_ROWS)
        ci = paired_ratio_ci(paired, seed=42)
        assert len(ci) > 0
        for row in ci:
            assert row["median"] > 0

    def test_repetitions_are_collapsed_by_median(self):
        paired = paired_compression_ratios(SYNTHETIC_ROWS)
        row = next(
            item
            for item in paired
            if item["sample_id"] == "sa"
            and item["transport"] == "tls13"
            and item["condition"] == "longllmlingua_2x"
        )
        assert row["bytes_total_ratio"] == pytest.approx(6450 / 12050, abs=1e-6)

    def test_ci_preserves_sample_ids(self):
        paired = paired_compression_ratios(SYNTHETIC_ROWS)
        ci = paired_ratio_ci(paired, seed=42)
        row = next(
            item
            for item in ci
            if item["transport"] == "tls13"
            and item["condition"] == "longllmlingua_2x"
            and item["metric"] == "bytes_total_ratio"
        )
        assert row["ci_low"] < row["ci_high"]

    def test_subsecond_latency_ratio_is_not_clamped(self):
        rows = [
            {
                "completed": True,
                "sample_id": "fast",
                "condition": "no_compression",
                "transport": "tls13",
                "elapsed_seconds": 0.5,
            },
            {
                "completed": True,
                "sample_id": "fast",
                "condition": "compressed",
                "transport": "tls13",
                "elapsed_seconds": 0.25,
            },
        ]
        ratio = paired_compression_ratios(rows)[0]
        assert ratio["elapsed_seconds_ratio"] == 0.5


class TestProtocolRatios:
    def test_protocol_ratios(self):
        proto = protocol_ratios(SYNTHETIC_ROWS)
        assert len(proto) > 0
        for row in proto:
            assert "bytes_total_ratio" in row

    def test_protocol_ratios_are_unique_and_use_median_repetitions(self):
        proto = protocol_ratios(SYNTHETIC_ROWS)
        keys = [
            (row["workload"], row["sample_id"], row["condition"])
            for row in proto
        ]
        assert len(keys) == len(set(keys))
        row = next(item for item in proto if item["condition"] == "no_compression")
        assert row["bytes_total_ratio"] == pytest.approx(10000 / 12050, abs=1e-6)

    def test_subsecond_protocol_latency_ratio_is_not_clamped(self):
        rows = [
            {
                "completed": True,
                "sample_id": "fast",
                "condition": "no_compression",
                "transport": "tls13",
                "elapsed_seconds": 0.5,
            },
            {
                "completed": True,
                "sample_id": "fast",
                "condition": "no_compression",
                "transport": "http3",
                "elapsed_seconds": 0.25,
            },
        ]
        ratio = protocol_ratios(rows)[0]
        assert ratio["elapsed_seconds_ratio"] == 0.5


class TestFinishReasons:
    def test_counts_correctly(self):
        fr = finish_reason_summary(SYNTHETIC_ROWS)
        assert fr["total_rows"] == len(SYNTHETIC_ROWS)
        assert fr["completed"] == 10  # 12 rows, 1 failed, 1 excluded
        assert fr["failed"] == 1
        assert fr["transport_failures"] == 1  # timeout
        assert fr["finish_reason_counts"].get("stop") == 10


class TestGenerateReport:
    def test_generates_all_csv_files(self):
        import json
        tmp = Path(tempfile.mkdtemp())
        results_path = tmp / "results.jsonl"
        with open(results_path, "w") as f:
            for index, row in enumerate(SYNTHETIC_ROWS):
                row = _with_capture_provenance(tmp, row, index)
                f.write(json.dumps(row) + "\n")

        outputs = generate_report(results_path, tmp / "report", seed=42)
        assert "group_medians" in outputs
        assert "paired_ratios" in outputs
        assert "paired_ratio_ci" in outputs
        assert "protocol_ratios" in outputs
        assert "scatter" in outputs
        assert "direction_medians" in outputs
        assert "ecdf" in outputs
        assert "marker" in outputs
        assert "rejected_trials" in outputs

        marker = (tmp / "report" / "REPORT_GENERATED").read_text()
        assert "rows_analyzed=11" in marker
        assert "completed=10" in marker
        assert "rejected=1" in marker

    def test_empty_results_does_not_crash(self):
        tmp = Path(tempfile.mkdtemp())
        results_path = tmp / "empty.jsonl"
        results_path.write_text("")
        outputs = generate_report(results_path, tmp / "report", seed=42)
        # Should complete without error
        assert "marker" in outputs

    def test_missing_capture_metrics_do_not_become_zero_byte_rows(self):
        import json

        tmp = Path(tempfile.mkdtemp())
        results_path = tmp / "results.jsonl"
        results_path.write_text(
            json.dumps(
                {
                    "completed": True,
                    "sample_id": "missing-capture",
                    "request_id": "missing-capture",
                    "condition": "no_compression",
                    "transport": "tls13",
                    "elapsed_seconds": 1.0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        outputs = generate_report(results_path, tmp / "report", seed=42)
        assert "scatter" not in outputs
        assert "direction_medians" not in outputs
        rejected = outputs["rejected_trials"].read_text(encoding="utf-8")
        assert "capture truncation was not explicitly cleared" in rejected

    def test_runner_jsonl_is_enriched_from_capture(self, monkeypatch):
        import csv
        import json

        tmp = Path(tempfile.mkdtemp())
        capture = tmp / "capture.pcapng"
        capture.write_bytes(b"synthetic capture placeholder")
        results_path = tmp / "results.jsonl"
        results_path.write_text(
            json.dumps(
                {
                    "completed": True,
                    "sample_id": "captured",
                    "request_id": "captured",
                    "condition": "no_compression",
                    "transport": "tls13",
                    "elapsed_seconds": 1.0,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "capture_file": capture.name,
                    "capture_sha256": hashlib.sha256(
                        capture.read_bytes()
                    ).hexdigest(),
                    "capture_return_code": 0,
                    "capture_may_be_truncated": False,
                    "negotiated_http_version": "HTTP/1.1",
                    "backend_port": 8443,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "traffic_experiment.traffic_measure.report.summarize_capture",
            lambda *_args, **_kwargs: {
                "bytes_client_to_server": 1024,
                "bytes_server_to_client": 1024,
                "bytes_total": 2048,
                "packets_total": 4,
                "tcp_packets": 4,
                "udp_packets": 0,
                "tls_records": 4,
            },
        )

        outputs = generate_report(results_path, tmp / "report", seed=42)

        with outputs["scatter"].open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        assert float(rows[0]["total_kib"]) == 2.0

    def test_scatter_collapses_technical_repetitions(self):
        import csv
        import json

        tmp = Path(tempfile.mkdtemp())
        results_path = tmp / "results.jsonl"
        repetitions = [
            _with_capture_provenance(
                tmp,
                {
                    "completed": True,
                    "sample_id": "sample-a",
                    "request_id": f"sample-a-{index}",
                    "condition": "no_compression",
                    "transport": "tls13",
                    "input_tokens": 100,
                    "elapsed_seconds": elapsed,
                    "bytes_client_to_server": upload,
                    "bytes_server_to_client": 1024,
                    "bytes_total": upload + 1024,
                },
                index,
            )
            for index, (elapsed, upload) in enumerate(
                [(1.0, 1024), (3.0, 3072)],
                start=1,
            )
        ]
        results_path.write_text(
            "".join(json.dumps(row) + "\n" for row in repetitions),
            encoding="utf-8",
        )

        outputs = generate_report(results_path, tmp / "report", seed=42)

        with outputs["scatter"].open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        assert rows[0]["technical_repetitions"] == "2"
        assert float(rows[0]["upload_kib"]) == 2.0
        assert float(rows[0]["latency_seconds"]) == 2.0

    def test_invalid_capture_and_protocol_rows_are_rejected(self):
        import csv
        import json

        tmp = Path(tempfile.mkdtemp())
        rows = []
        for index, request_id in enumerate(
            ("truncated", "wrong-version", "tcp-fallback"),
            start=1,
        ):
            row = _with_capture_provenance(
                tmp,
                {
                    "completed": True,
                    "sample_id": request_id,
                    "request_id": request_id,
                    "condition": "no_compression",
                    "transport": "http3",
                    "elapsed_seconds": 1.0,
                    "bytes_total": 100,
                    "bytes_client_to_server": 50,
                    "bytes_server_to_client": 50,
                },
                index,
            )
            rows.append(row)
        rows[0]["capture_may_be_truncated"] = True
        rows[1]["negotiated_http_version"] = "HTTP/1.1"
        rows[2]["tcp_packets"] = 1
        results_path = tmp / "results.jsonl"
        results_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

        outputs = generate_report(results_path, tmp / "report", seed=42)

        with outputs["rejected_trials"].open(
            newline="",
            encoding="utf-8",
        ) as handle:
            rejected = list(csv.DictReader(handle))
        assert len(rejected) == 3
        reasons = {row["analysis_rejection_reason"] for row in rejected}
        assert any("truncation" in reason for reason in reasons)
        assert any("did not negotiate HTTP/3" in reason for reason in reasons)
        assert any("TCP fallback" in reason for reason in reasons)
