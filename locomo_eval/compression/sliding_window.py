from __future__ import annotations

import logging

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.schemas import Turn

LOGGER = logging.getLogger(__name__)


class SlidingWindowCompressor(BaseCompressor):
    name = "sliding_window"

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        if budget is None:
            return build_result(list(turns), format_and_count_fn, {"budget": None})
        kept_reversed: list[Turn] = []
        for turn in reversed(turns):
            candidate = list(reversed([*kept_reversed, turn]))
            _, token_count = format_and_count_fn(candidate, None)
            if token_count <= budget:
                kept_reversed.append(turn)
                continue
            if not kept_reversed:
                LOGGER.warning("budget=%s smaller than smallest turn; keeping most recent turn %s", budget, turn.dia_id)
                kept_reversed.append(turn)
            break
        kept = list(reversed(kept_reversed))
        return build_result(kept, format_and_count_fn, {"budget": budget})
