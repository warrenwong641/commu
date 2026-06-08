from __future__ import annotations

from pathlib import Path

from locomo_eval.experiments.config import ExperimentConfig
from locomo_eval.experiments.runner import ExperimentRunner


class DummyAdapter:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def generate(self, messages, max_new_tokens=256):
        class Result:
            text = "Taipei"
        return Result()


def test_runner_outputs_rows(tmp_path, mock_conversation, simple_tokenizer):
    config = ExperimentConfig(
        model="dummy",
        compression_methods=["no_compression", "oracle_evidence"],
        budgets=[32],
        output_dir=str(tmp_path),
        max_samples=2,
    )
    runner = ExperimentRunner(config, DummyAdapter(simple_tokenizer))
    rows = runner.run([mock_conversation], dry_run=True)
    assert len(rows) == 2
    assert (Path(tmp_path) / "results.parquet").exists()
