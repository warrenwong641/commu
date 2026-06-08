from __future__ import annotations

import math


def summarize_perplexity(perplexity_payload: dict) -> dict:
    if not perplexity_payload or "perplexity" not in perplexity_payload:
        return {
            "perplexity": None,
            "nll": None,
            "token_nlls": [],
            "answer_tokens": [],
            "scored_answer_tokens": 0,
            "answer_tokens_total": 0,
            "context_tokens_used": 0,
            "context_tokens_dropped": 0,
        }
    return {
        "perplexity": float(perplexity_payload["perplexity"]) if perplexity_payload.get("perplexity") is not None else None,
        "nll": float(perplexity_payload["nll"]) if perplexity_payload.get("nll") is not None else None,
        "token_nlls": perplexity_payload.get("token_nlls", []) or [],
        "answer_tokens": perplexity_payload.get("answer_tokens", []) or [],
        "scored_answer_tokens": int(perplexity_payload.get("scored_answer_tokens", 0) or 0),
        "answer_tokens_total": int(perplexity_payload.get("answer_tokens_total", 0) or 0),
        "context_tokens_used": int(perplexity_payload.get("context_tokens_used", 0) or 0),
        "context_tokens_dropped": int(perplexity_payload.get("context_tokens_dropped", 0) or 0),
    }


def detect_high_perplexity_tokens(perplexity_payload: dict, threshold: float = 2.0) -> list[dict]:
    token_nlls = perplexity_payload.get("token_nlls", []) or []
    answer_tokens = perplexity_payload.get("answer_tokens", []) or []
    high: list[dict] = []
    for token, nll_val in zip(answer_tokens, token_nlls):
        nll = float(nll_val)
        if nll > threshold:
            high.append({"token": token, "nll": nll})
    return high
