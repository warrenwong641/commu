from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ExperimentConfig:
    model: str
    compression_methods: list[str]
    budgets: list[int | str | None]  # int=absolute tokens, str="50%"=percentage, None=full
    output_dir: str
    max_samples: int | None = None
    system_prompt: str = (
        "You are Qwen, a helpful AI assistant. Answer the following question based on the conversation history provided. "
        "Only use information from the conversation. If the conversation does not contain enough information to answer, say so."
    )
    seed: int = 42
    last_k_default: int = 20
    hybrid_allocation: dict[str, float] = field(default_factory=lambda: {"recent_ratio": 0.3, "retrieval_ratio": 0.4, "summary_ratio": 0.3})
    session_summary_source: str = "dataset"  # "dataset" or "generated"

    @classmethod
    def from_yaml(cls, file_path: str | Path) -> "ExperimentConfig":
        with Path(file_path).open("r", encoding="utf-8") as handle:
            raw: dict[str, Any] = yaml.safe_load(handle)
        return cls(**raw)

    def resolve_budget(self, budget: int | str | None, original_tokens: int) -> int | None:
        if budget is None:
            return None
        if isinstance(budget, str) and budget.endswith("%"):
            pct = float(budget.rstrip("%")) / 100.0
            return max(1, int(original_tokens * pct))
        return int(budget)
