from __future__ import annotations

import numpy as np

from .base import BaseCompressor, CompressedResult, build_result
from locomo_eval.locomo.qa_builder import ConversationPrecomputed
from locomo_eval.locomo.schemas import Turn


class RetrievalCompressor(BaseCompressor):
    """TF-IDF cosine similarity retrieval over conversation turns.

    Uses sklearn TfidfVectorizer (L2-normalized rows) with dot-product scoring,
    which is equivalent to cosine similarity. Precomputed per conversation for speed.
    This is TF-IDF cosine, not BM25 — it lacks BM25's term-frequency saturation
    and document-length normalization.
    """

    name = "retrieval"

    def __init__(self, conversation_precomputed: ConversationPrecomputed, top_k: int | None = None, candidate_k: int = 64) -> None:
        self.conversation_precomputed = conversation_precomputed
        self.top_k = top_k
        self.candidate_k = candidate_k

    def retrieve(self, question: str, limit: int | None = None) -> list[Turn]:
        vector = self.conversation_precomputed.tfidf_vectorizer.transform([question])
        scores = (self.conversation_precomputed.tfidf_matrix @ vector.T).toarray().ravel()
        effective_limit = limit if limit is not None else self.top_k or self.candidate_k
        top_indices = np.argsort(scores)[-effective_limit:][::-1]
        turns = self.conversation_precomputed.conversation.all_turns
        return [turns[int(index)] for index in top_indices if scores[int(index)] > 0]

    def compress(self, turns: list[Turn], question: str, budget: int | None, format_and_count_fn) -> CompressedResult:
        if budget is None:
            kept = self.retrieve(question, self.top_k or self.candidate_k)
            return build_result(sorted(kept, key=lambda turn: turns.index(turn)), format_and_count_fn, {"budget": None, "top_k": self.top_k, "candidate_k": self.candidate_k})
        kept: list[Turn] = []
        for turn in self.retrieve(question, self.candidate_k):
            candidate = kept + [turn]
            ordered_candidate = sorted(candidate, key=lambda item: turns.index(item))
            _, token_count = format_and_count_fn(ordered_candidate, None)
            if token_count <= budget:
                kept = ordered_candidate
        return build_result(kept, format_and_count_fn, {"top_k": self.top_k, "candidate_k": self.candidate_k, "budget": budget})
