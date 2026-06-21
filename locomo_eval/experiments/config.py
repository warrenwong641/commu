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
    sample_strategy: str = "sequential"  # "sequential" or "stratified"
    context_format: str = "chat"  # "chat" or "evidence"
    retrieval_top_k: int | None = None
    retrieval_candidate_k: int = 64
    neighbor_window_size: int = 1
    dense_retrieval_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    enable_llm_judge: bool = False
    attention_examples_per_method: int = 0
    attention_budget_labels: list[str] = field(default_factory=list)
    attention_layers: list[str | int] = field(default_factory=lambda: ["early", "mid", "late"])
    system_prompt: str = (
        "You are Qwen, a helpful AI assistant. Answer the following question based on the conversation history provided. "
        "Only use information from the conversation. If the conversation does not contain enough information to answer, say so."
    )
    seed: int = 42
    last_k_default: int = 20
    hybrid_allocation: dict[str, float] = field(default_factory=lambda: {"recent_ratio": 0.3, "retrieval_ratio": 0.4, "summary_ratio": 0.3})
    session_summary_source: str = "dataset"  # "dataset" or "generated"
    claude_recent_turns: int = 4
    claude_retrieval_turns: int = 2
    claude_max_summary_turns: int = 12
    claude_summary_preview_chars: int = 120
    claude_stub_preview_chars: int = 80

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
