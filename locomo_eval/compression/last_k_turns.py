from __future__ import annotations

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.schemas import Turn


class LastKTurnsCompressor(BaseCompressor):
    name = "last_k_turns"

    def __init__(self, k: int = 20) -> None:
        self.k = k

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        kept = [] if self.k <= 0 else list(turns[-self.k :])
        return build_result(kept, format_and_count_fn, {"k": self.k, "budget": budget})
