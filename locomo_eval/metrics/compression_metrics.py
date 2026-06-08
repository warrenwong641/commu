from __future__ import annotations


def compression_ratio(original_tokens: int, compressed_tokens: int) -> float:
    if original_tokens == 0:
        return 0.0
    return compressed_tokens / original_tokens
