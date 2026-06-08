from __future__ import annotations


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
