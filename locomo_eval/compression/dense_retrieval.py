from __future__ import annotations

import numpy as np

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.formatter import format_turn_as_evidence
from locomo_eval.locomo.schemas import Turn


class DenseRetrievalCompressor(BaseCompressor):
    name = "dense_retrieval"

    def __init__(
        self,
        turns: list[Turn],
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        top_k: int | None = None,
        candidate_k: int = 64,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.turns = turns
        self.top_k = top_k
        self.candidate_k = candidate_k
        self.model = SentenceTransformer(model_name)
        turn_texts = [format_turn_as_evidence(turn) for turn in turns]
        self.turn_embeddings = self.model.encode(turn_texts, normalize_embeddings=True)

    def retrieve(self, question: str, limit: int | None = None) -> list[Turn]:
        effective_limit = limit if limit is not None else self.top_k or self.candidate_k
        query_embedding = self.model.encode([question], normalize_embeddings=True)[0]
        scores = np.asarray(self.turn_embeddings) @ np.asarray(query_embedding)
        top_indices = np.argsort(scores)[-effective_limit:][::-1]
        return [self.turns[int(index)] for index in top_indices]

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
