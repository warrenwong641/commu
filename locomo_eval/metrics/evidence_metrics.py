from __future__ import annotations

from locomo_eval.locomo.schemas import Turn
from locomo_eval.metrics.answer_metrics import normalize_answer


def evidence_recall(kept_ids: list[str], evidence_ids: list[str]) -> float:
    if not evidence_ids:
        return 1.0
    kept = set(kept_ids)
    hits = sum(1 for item in evidence_ids if item in kept)
    return hits / len(evidence_ids)


def evidence_precision(kept_ids: list[str], evidence_ids: list[str]) -> float:
    if not kept_ids:
        return 0.0
    evidence = set(evidence_ids)
    hits = sum(1 for item in kept_ids if item in evidence)
    return hits / len(kept_ids)


def token_survival_rate(original_tokens: int, compressed_tokens: int) -> float:
    if original_tokens == 0:
        return 0.0
    return compressed_tokens / original_tokens


def evidence_session_recall(kept_turns: list[Turn], all_turns: list[Turn], evidence_ids: list[str]) -> float:
    if not evidence_ids:
        return 1.0
    turn_by_id = {turn.dia_id: turn for turn in all_turns}
    evidence_sessions = {
        turn_by_id[evidence_id].session_id
        for evidence_id in evidence_ids
        if evidence_id in turn_by_id and turn_by_id[evidence_id].session_id is not None
    }
    if not evidence_sessions:
        return 0.0
    kept_sessions = {turn.session_id for turn in kept_turns if turn.session_id is not None}
    return len(evidence_sessions & kept_sessions) / len(evidence_sessions)


def answerable_context_rate(kept_turns: list[Turn], reference_answer: str, extra_context: str | None = None) -> float:
    normalized_reference = normalize_answer(reference_answer)
    if not normalized_reference:
        return 0.0
    context_parts = [turn.text for turn in kept_turns]
    if extra_context:
        context_parts.append(extra_context)
    normalized_context = normalize_answer("\n".join(context_parts))
    return float(normalized_reference in normalized_context)
