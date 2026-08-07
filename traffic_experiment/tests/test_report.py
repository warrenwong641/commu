"""Tests for the comprehensive report module using synthetic fixtures only."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from traffic_measure.report import (
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


class TestProtocolRatios:
    def test_protocol_ratios(self):
        proto = protocol_ratios(SYNTHETIC_ROWS)
        assert len(proto) > 0
        for row in proto:
            assert "bytes_total_ratio" in row


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
            for row in SYNTHETIC_ROWS:
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

        marker = (tmp / "report" / "REPORT_GENERATED").read_text()
        assert "rows_analyzed=11" in marker
        assert "completed=10" in marker

    def test_empty_results_does_not_crash(self):
        tmp = Path(tempfile.mkdtemp())
        results_path = tmp / "empty.jsonl"
        results_path.write_text("")
        outputs = generate_report(results_path, tmp / "report", seed=42)
        # Should complete without error
        assert "marker" in outputs
