from __future__ import annotations

from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.locomo.formatter import make_format_and_count_fn
from locomo_eval.locomo.qa_builder import precompute_retrieval
from locomo_eval.metrics.evidence_metrics import evidence_recall


def test_last_k_zero(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    result = LastKTurnsCompressor(0).compress(mock_conversation.all_turns, "q", 32, fn)
    assert result.kept_turns == []


def test_sliding_window_budget(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    result = SlidingWindowCompressor().compress(mock_conversation.all_turns, "q", 8, fn)
    assert len(result.kept_turns) >= 1


def test_oracle_recall(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    result = OracleEvidenceCompressor(["1", "7"]).compress(mock_conversation.all_turns, "q", 64, fn)
    assert evidence_recall(result.kept_turn_ids, ["1", "7"]) == 1.0


def test_hybrid_dedup(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    precomputed = precompute_retrieval(mock_conversation)
    result = HybridCompressor(RetrievalCompressor(precomputed), recent_k=3).compress(mock_conversation.all_turns, "teacher", 64, fn)
    assert len(result.kept_turn_ids) == len(set(result.kept_turn_ids))
