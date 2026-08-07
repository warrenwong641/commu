from __future__ import annotations

import json
from pathlib import Path

from locomo_eval.experiments.config import ExperimentConfig
from locomo_eval.experiments.runner import ExperimentRunner
from locomo_eval.experimental.claude_context import ClaudeContextCompressor
from locomo_eval.locomo.qa_builder import precompute_retrieval


class DummyAdapter:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.generate_calls = 0

    def generate(self, messages, max_new_tokens=256):
        self.generate_calls += 1

        class Result:
            text = "Taipei"
        return Result()

    def compute_perplexity(self, messages, answer_text):
        return {"perplexity": 2.5, "nll": 0.9, "token_nlls": [], "answer_tokens": []}


def test_runner_outputs_rows(tmp_path, mock_conversation, simple_tokenizer):
    config = ExperimentConfig(
        model="dummy",
        compression_methods=["no_compression", "oracle_evidence"],
        budgets=[32],
        output_dir=str(tmp_path),
        max_samples=1,  # limits to 1 QA
    )
    runner = ExperimentRunner(config, DummyAdapter(simple_tokenizer))
    rows = runner.run([mock_conversation], dry_run=False)
    # 1 QA × 2 methods × 1 budget = 2 rows
    assert len(rows) == 2
    assert (Path(tmp_path) / "results.parquet").exists()
    assert "perplexity" in rows[0]
    assert "latency" in rows[0]
    assert "gpu_memory_mb" in rows[0]
    assert "token_saving" in rows[0]
    assert "budget_label" in rows[0]


def test_runner_builds_experimental_claude_context(mock_conversation, simple_tokenizer):
    config = ExperimentConfig(
        model="dummy",
        compression_methods=["claude_context"],
        budgets=[128],
        output_dir="unused",
        method_options={"claude_context": {"recent_turns": 2, "cache_prefix_turns": 1}},
    )
    runner = ExperimentRunner(config, DummyAdapter(simple_tokenizer))
    compressor = runner._build_compressor("claude_context", precompute_retrieval(mock_conversation), [])

    assert isinstance(compressor, ClaudeContextCompressor)
    assert compressor.policy.recent_turns == 2
    assert compressor.policy.cache_prefix_turns == 1


def test_runner_persists_claude_context_audit_data(tmp_path, mock_conversation, simple_tokenizer):
    config = ExperimentConfig(
        model="dummy",
        compression_methods=["claude_context"],
        budgets=[96],
        output_dir=str(tmp_path),
        max_samples=1,
        context_format="evidence",
        method_options={
            "claude_context": {
                "recent_turns": 1,
                "retrieval_turns": 0,
                "max_summary_turns": 2,
                "cache_prefix_turns": 1,
            }
        },
    )
    runner = ExperimentRunner(config, DummyAdapter(simple_tokenizer))

    row = runner.run([mock_conversation])[0]
    metadata = json.loads(row["compression_metadata"])

    assert row["extra_context"] is not None
    assert "CLAUDE_CONTEXT_BLOCK" in row["extra_context"]
    assert metadata["applied_edits"]
    assert metadata["compaction_source_turn_ids"]
    assert metadata["clearing_policy"]["cache_prefix_turns"] == 1
    assert "cache_invalidated_by_emergency" in metadata
    assert row["compression_budget_satisfied"] is True
    assert row["compression_error"] is None
    assert row["valid_for_analysis"] is True


def test_runner_records_unachievable_context_budget_without_generation(
    tmp_path,
    mock_conversation,
    simple_tokenizer,
):
    config = ExperimentConfig(
        model="dummy",
        compression_methods=["claude_context"],
        budgets=[1],
        output_dir=str(tmp_path),
        max_samples=1,
        context_format="evidence",
        method_options={
            "claude_context": {
                "recent_turns": 1,
                "min_recent_turns": 1,
                "retrieval_turns": 0,
            }
        },
    )
    adapter = DummyAdapter(simple_tokenizer)
    runner = ExperimentRunner(config, adapter)

    row = runner.run([mock_conversation])[0]
    metadata = json.loads(row["compression_metadata"])

    assert adapter.generate_calls == 0
    assert row["prediction"] is None
    assert row["compression_budget_satisfied"] is False
    assert row["valid_for_analysis"] is False
    assert row["error"].startswith("COMPRESSION_BUDGET_EXCEEDED:")
    assert row["compressed_tokens"] > row["budget"]
    assert metadata["budget_satisfied"] is False
    assert metadata["budget_error"] == row["compression_error"]
