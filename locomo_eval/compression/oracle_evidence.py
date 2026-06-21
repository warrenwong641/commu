from __future__ import annotations

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.schemas import Turn


class OracleEvidenceCompressor(BaseCompressor):
    name = "oracle_evidence"

    def __init__(self, evidence_ids: list[str]) -> None:
        self.evidence_ids = set(evidence_ids)

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        kept = [turn for turn in turns if turn.dia_id in self.evidence_ids]
        return build_result(kept, format_and_count_fn, {"budget": budget, "evidence_ids": sorted(self.evidence_ids)})
