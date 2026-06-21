from __future__ import annotations

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.schemas import Turn


class NeighborWindowCompressor(BaseCompressor):
    name = "neighbor_window"

    def __init__(self, base_compressor: BaseCompressor, window_size: int = 1) -> None:
        self.base_compressor = base_compressor
        self.window_size = window_size

    def _expand(self, turns: list[Turn], selected: list[Turn]) -> list[Turn]:
        selected_ids = {turn.dia_id for turn in selected}
        by_id = {turn.dia_id: idx for idx, turn in enumerate(turns)}
        expanded_indices: set[int] = set()
        for turn_id in selected_ids:
            if turn_id not in by_id:
                continue
            center = by_id[turn_id]
            start = max(0, center - self.window_size)
            end = min(len(turns), center + self.window_size + 1)
            expanded_indices.update(range(start, end))
        return [turns[idx] for idx in sorted(expanded_indices)]

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        seed = self.base_compressor.compress(turns, question, None, format_and_count_fn)
        candidates = self._expand(turns, seed.kept_turns)
        if budget is None:
            return build_result(candidates, format_and_count_fn, {"base": self.base_compressor.name, "window_size": self.window_size, "budget": None})

        kept: list[Turn] = []
        for turn in candidates:
            candidate = kept + [turn]
            _, token_count = format_and_count_fn(candidate, None)
            if token_count <= budget:
                kept = candidate
        return build_result(kept, format_and_count_fn, {"base": self.base_compressor.name, "window_size": self.window_size, "budget": budget})
