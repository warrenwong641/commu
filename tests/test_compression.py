from __future__ import annotations

from locomo_eval.compression.bm25 import BM25Compressor
from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.neighbor_window import NeighborWindowCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.session_summary import SessionSummaryCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.locomo.formatter import build_chat_messages, make_format_and_count_fn
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


def test_retrieval_uses_candidate_pool_to_fill_budget(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    precomputed = precompute_retrieval(mock_conversation)
    result = RetrievalCompressor(precomputed, top_k=1, candidate_k=4).compress(mock_conversation.all_turns, "Alice Taipei noodles", 64, fn)
    assert len(result.kept_turn_ids) > 1


def test_bm25_retrieves_matching_turn(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    result = BM25Compressor(mock_conversation.all_turns, candidate_k=4).compress(mock_conversation.all_turns, "train leaves", 64, fn)
    assert "7" in result.kept_turn_ids or "8" in result.kept_turn_ids


def test_neighbor_window_expands_retrieval_context(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    base = BM25Compressor(mock_conversation.all_turns, top_k=1, candidate_k=1)
    result = NeighborWindowCompressor(base, window_size=1).compress(mock_conversation.all_turns, "train leaves", 128, fn)
    assert len(result.kept_turn_ids) >= 2


def test_session_summary_exposes_extra_context(mock_conversation, simple_tokenizer):
    summary = "Alice lives in Taipei and likes noodles."
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    result = SessionSummaryCompressor(summary_text=summary).compress(mock_conversation.all_turns, "q", 64, fn)
    assert result.extra_context == summary
    assert result.kept_turns == []
    # Verify summary appears in messages when wired like runner does
    messages = build_chat_messages(result.kept_turns, "Alice", "Bob", system_prompt="Be helpful.")
    if result.extra_context:
        messages.insert(1, {"role": "system", "content": result.extra_context})
    assert any(summary in m["content"] for m in messages)
    assert len(messages) >= 2  # system prompt + summary
