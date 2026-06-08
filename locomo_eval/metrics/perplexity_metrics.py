from __future__ import annotations


def summarize_perplexity(perplexity_payload: dict) -> dict:
    return {
        "perplexity": float(perplexity_payload["perplexity"]),
        "nll": float(perplexity_payload["nll"]),
    }
