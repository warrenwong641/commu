from __future__ import annotations

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.schemas import Turn


class NoCompressionCompressor(BaseCompressor):
    name = "no_compression"

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        return build_result(list(turns), format_and_count_fn, {"budget": budget})
