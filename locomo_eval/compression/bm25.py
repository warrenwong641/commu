from __future__ import annotations

from collections import Counter
import math
import re

import numpy as np

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.formatter import format_turn_as_evidence
from locomo_eval.locomo.schemas import Turn


_TOKEN_PATTERN = re.compile(r"\b\w+\b")


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_PATTERN.findall(text)]


class BM25Compressor(BaseCompressor):
    name = "bm25"

    def __init__(self, turns: list[Turn], top_k: int | None = None, candidate_k: int = 64, k1: float = 1.5, b: float = 0.75) -> None:
        self.turns = turns
        self.top_k = top_k
        self.candidate_k = candidate_k
        self.k1 = k1
        self.b = b
        self.docs = [_tokenize(format_turn_as_evidence(turn)) for turn in turns]
        self.doc_lens = np.asarray([len(doc) for doc in self.docs], dtype=float)
        self.avg_doc_len = float(self.doc_lens.mean()) if len(self.doc_lens) else 0.0
        self.term_counts = [Counter(doc) for doc in self.docs]
        doc_freq: Counter[str] = Counter()
        for doc in self.docs:
            doc_freq.update(set(doc))
        n_docs = max(1, len(self.docs))
        self.idf = {
            term: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    def retrieve(self, question: str, limit: int | None = None) -> list[Turn]:
        query_terms = _tokenize(question)
        if not query_terms:
            return []
        scores = np.zeros(len(self.turns), dtype=float)
        for idx, counts in enumerate(self.term_counts):
            doc_len = self.doc_lens[idx] if len(self.doc_lens) else 0.0
            denom_norm = self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_len)) if self.avg_doc_len else self.k1
            score = 0.0
            for term in query_terms:
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                score += self.idf.get(term, 0.0) * ((tf * (self.k1 + 1)) / (tf + denom_norm))
            scores[idx] = score
        effective_limit = limit if limit is not None else self.top_k or self.candidate_k
        top_indices = np.argsort(scores)[-effective_limit:][::-1]
        return [self.turns[int(index)] for index in top_indices if scores[int(index)] > 0]

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        if budget is None:
            kept = self.retrieve(question, self.top_k or self.candidate_k)
            return build_result(sorted(kept, key=lambda turn: turns.index(turn)), format_and_count_fn, {"budget": None, "top_k": self.top_k, "candidate_k": self.candidate_k})

        kept: list[Turn] = []
        for turn in self.retrieve(question, self.candidate_k):
            candidate = sorted(kept + [turn], key=lambda item: turns.index(item))
            _, token_count = format_and_count_fn(candidate, None)
            if token_count <= budget:
                kept = candidate
        return build_result(kept, format_and_count_fn, {"top_k": self.top_k, "candidate_k": self.candidate_k, "budget": budget})
