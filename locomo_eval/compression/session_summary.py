from __future__ import annotations

from dataclasses import dataclass

from .base import BaseCompressor, CompressedResult
from locomo_eval.locomo.schemas import Turn


@dataclass
class SessionSummaryCompressor(BaseCompressor):
    name = "session_summary"
    summary_text: str = ""

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        extra = self.summary_text or None
        text, token_count = format_and_count_fn([], extra)
        return CompressedResult(
            kept_turns=[],
            kept_turn_ids=[],
            compressed_context=text,
            token_count=token_count,
            extra_context=extra,
            metadata={"budget": budget, "summary_used": bool(self.summary_text)},
        )
