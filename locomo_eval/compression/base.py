from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from locomo_eval.locomo.schemas import Turn


FormatAndCountFn = Callable[[list[Turn], str | None], tuple[str, int]]


@dataclass
class CompressedResult:
    kept_turns: list[Turn]
    kept_turn_ids: list[str]
    compressed_context: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    extra_context: str | None = None


class BaseCompressor(ABC):
    name = "base"

    @abstractmethod
    def compress(
        self,
        turns: list[Turn],
        question: str,
        budget: int | None,
        format_and_count_fn: FormatAndCountFn,
    ) -> CompressedResult:
        raise NotImplementedError


def build_result(kept_turns: list[Turn], format_and_count_fn: FormatAndCountFn, metadata: dict[str, Any] | None = None) -> CompressedResult:
    text, token_count = format_and_count_fn(kept_turns, None)
    return CompressedResult(
        kept_turns=kept_turns,
        kept_turn_ids=[turn.dia_id for turn in kept_turns],
        compressed_context=text,
        token_count=token_count,
        metadata=metadata or {},
    )
