from __future__ import annotations

from traffic_experiment.traffic_measure.vllm_metrics import (
    diff_vllm_snapshots,
    parse_prometheus_totals,
)


def test_parse_prometheus_totals_aggregates_label_sets():
    totals = parse_prometheus_totals(
        """
# HELP vllm:prompt_tokens_total Prompt tokens
vllm:prompt_tokens_total{model_name="a"} 10
vllm:prompt_tokens_total{model_name="b"} 5
vllm:generation_tokens_total 7
ignored_without_value
"""
    )
    assert totals["vllm:prompt_tokens_total"] == 15
    assert totals["vllm:generation_tokens_total"] == 7


def test_diff_vllm_snapshots_calculates_counter_rates():
    before = {
        "captured_at_utc": "2026-01-01T00:00:00Z",
        "captured_at_epoch_seconds": 100.0,
        "metrics": {
            "vllm:prompt_tokens_total": 100,
            "vllm:generation_tokens_total": 20,
        },
    }
    after = {
        "captured_at_utc": "2026-01-01T00:00:10Z",
        "captured_at_epoch_seconds": 110.0,
        "metrics": {
            "vllm:prompt_tokens_total": 300,
            "vllm:generation_tokens_total": 70,
        },
    }
    result = diff_vllm_snapshots(before, after)
    assert result["counter_deltas"]["vllm:prompt_tokens_total"] == 200
    assert (
        result["counter_rates"]["vllm:generation_tokens_total_per_second"]
        == 5
    )
