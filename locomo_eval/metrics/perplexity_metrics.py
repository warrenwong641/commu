from __future__ import annotations

import math


def summarize_perplexity(perplexity_payload: dict) -> dict:
    if not perplexity_payload or "perplexity" not in perplexity_payload:
        return {"perplexity": None, "nll": None}
    return {
        "perplexity": float(perplexity_payload["perplexity"]),
        "nll": float(perplexity_payload["nll"]),
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
