from __future__ import annotations

from dataclasses import dataclass

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.locomo.schemas import Turn


@dataclass
class HybridCompressor(BaseCompressor):
    name = "hybrid"
    retrieval_compressor: RetrievalCompressor
    recent_k: int = 8

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        if budget is None:
            recent = turns[-self.recent_k :]
            retrieved = self.retrieval_compressor.retrieve(question)
        else:
            recent = turns[-self.recent_k :]
            retrieved = self.retrieval_compressor.retrieve(question)
        seen: set[str] = set()
        kept: list[Turn] = []
        for turn in recent + retrieved:
            if turn.dia_id in seen:
                continue
            candidate = kept + [turn]
            if budget is None:
                kept = candidate
                seen.add(turn.dia_id)
                continue
            _, token_count = format_and_count_fn(sorted(candidate, key=lambda item: turns.index(item)), None)
            if token_count <= budget:
                kept = candidate
                seen.add(turn.dia_id)
        kept = sorted(kept, key=lambda item: turns.index(item))
        return build_result(kept, format_and_count_fn, {"budget": budget, "recent_k": self.recent_k})
