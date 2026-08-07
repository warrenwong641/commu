from __future__ import annotations

from locomo_eval.compression.bm25 import BM25Compressor
from locomo_eval.compression.hybrid import HybridCompressor
from locomo_eval.compression.last_k_turns import LastKTurnsCompressor
from locomo_eval.compression.oracle_evidence import OracleEvidenceCompressor
from locomo_eval.compression.neighbor_window import NeighborWindowCompressor
from locomo_eval.compression.retrieval import RetrievalCompressor
from locomo_eval.compression.session_summary import SessionSummaryCompressor
from locomo_eval.compression.sliding_window import SlidingWindowCompressor
from locomo_eval.experimental.claude_context import ClaudeContextCompressor, ClaudeContextPolicy
from locomo_eval.locomo.formatter import build_chat_messages, make_format_and_count_fn
from locomo_eval.locomo.qa_builder import precompute_retrieval
from locomo_eval.locomo.schemas import Turn
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


def test_claude_context_emits_recoverable_server_side_items(simple_tokenizer):
    turns = [
        Turn(dia_id="1", speaker="Alice", text="I live in Taipei and the train leaves at 8 AM.", session_id="s1"),
        Turn(dia_id="2", speaker="Bob", text="[tool_result] search results with old logs and repeated command output.", session_id="s1"),
        Turn(dia_id="3", speaker="Bob", text="[thinking] private reasoning block that should be cleared.", session_id="s1"),
        Turn(dia_id="4", speaker="Alice", text="My brother Kevin is a teacher.", session_id="s1"),
        Turn(dia_id="5", speaker="Bob", text="Let's continue with the travel plan.", session_id="s2"),
        Turn(dia_id="6", speaker="Alice", text="Please remember the trip details.", session_id="s2"),
    ]
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=2, retrieval_turns=1, max_summary_turns=4, summary_preview_chars=80)
    )

    result = compressor.compress(turns, "Where does Alice live and when does the train leave?", 220, fn)

    assert result.kept_turn_ids[-2:] == ["5", "6"]
    assert "1" in result.kept_turn_ids
    assert "clear_tool_uses_20250919" in result.metadata["server_side_items"]
    assert "clear_thinking_20251015" in result.metadata["server_side_items"]
    assert "compact_20260112" in result.metadata["server_side_items"]
    assert result.metadata["artifact_stub_turn_ids"] == ["2"]
    assert result.metadata["thinking_cleared_turn_ids"] == ["3"]
    assert result.extra_context is not None
    assert "CLAUDE_CONTEXT_BLOCK" in result.extra_context
    assert "artifact_stub" in result.extra_context


def test_claude_context_tight_budget_shrinks_projection(simple_tokenizer):
    turns = [
        Turn(dia_id=str(index), speaker="Alice" if index % 2 else "Bob", text=f"Old state fact number {index}.", session_id="s1")
        for index in range(1, 10)
    ]
    turns.append(Turn(dia_id="10", speaker="Bob", text="Current task is still active.", session_id="s2"))
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=4, retrieval_turns=2, max_summary_turns=8, summary_preview_chars=60)
    )

    result = compressor.compress(turns, "What is the current task?", 95, fn)

    assert result.token_count <= 95
    assert result.kept_turn_ids[-1:] == ["10"]
    assert result.metadata["force_minimal_summary"] is True
    assert result.metadata["canonical_transcript_turns"] == 10
    assert result.metadata["budget_evicted_turn_ids"]


def test_claude_context_only_drops_stale_turns_that_are_summarized(simple_tokenizer):
    turns = [
        Turn(dia_id=str(index), speaker="Alice", text=f"Historical fact {index}.", session_id="s1")
        for index in range(1, 7)
    ]
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=1, retrieval_turns=0, max_summary_turns=2)
    )

    result = compressor.compress(turns, "Unrelated query", 500, fn)

    assert result.metadata["compaction_source_turn_ids"] == ["4", "5"]
    assert result.metadata["compaction_summary_turn_ids"] == ["4", "5"]
    assert result.metadata["budget_evicted_turn_ids"] == []
    assert result.kept_turn_ids == ["1", "2", "3", "6"]
    compact_edit = next(edit for edit in result.metadata["applied_edits"] if edit["type"] == "compact_20260112")
    assert compact_edit["turn_ids"] == ["4", "5"]


def test_claude_context_handles_empty_transcript(simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    result = ClaudeContextCompressor().compress([], "q", 64, fn)

    assert result.kept_turns == []
    assert result.metadata["mode"] == "empty_transcript"
    assert result.metadata["server_side_items"] == []
    assert result.extra_context is not None


def test_claude_context_cache_prefix_protects_stable_turn(simple_tokenizer):
    turns = [
        Turn(dia_id="1", speaker="Bob", text="[tool_result] stable cached setup output should stay byte stable.", session_id="s1"),
        Turn(dia_id="2", speaker="Alice", text="Old implementation detail.", session_id="s1"),
        Turn(dia_id="3", speaker="Bob", text="[tool_result] stale command output can be cleared.", session_id="s1"),
        Turn(dia_id="4", speaker="Alice", text="Current question is active.", session_id="s2"),
    ]
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=1, cache_prefix_turns=1, retrieval_turns=0)
    )

    result = compressor.compress(turns, "What is active now?", 220, fn)

    assert result.metadata["stable_prefix_turn_ids"] == ["1"]
    assert result.metadata["cache_invalidated_by_edits"] is False
    assert result.metadata["edits_before_cache_boundary"] == []
    assert "1" in result.kept_turn_ids
    assert result.metadata["artifact_stub_turn_ids"] == ["3"]


def test_claude_context_emergency_invalidation_evicts_stable_prefix(simple_tokenizer):
    turns = [
        Turn(
            dia_id=str(index),
            speaker="Alice",
            text=f"Old cached detail {index} with enough filler words to consume context.",
            session_id="s1",
        )
        for index in range(1, 9)
    ]
    turns.append(Turn(dia_id="9", speaker="Bob", text="Current task.", session_id="s2"))
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(
            recent_turns=1,
            retrieval_turns=0,
            max_summary_turns=2,
            cache_prefix_turns=2,
            allow_cache_invalidation_on_emergency=True,
        )
    )

    result = compressor.compress(turns, "What is current?", 75, fn)

    assert result.token_count <= 75
    assert result.metadata["force_minimal_summary"] is True
    assert result.metadata["stable_prefix_turn_ids"] == ["1", "2"]
    assert result.metadata["cache_invalidated_by_emergency"] is True
    assert result.metadata["cache_invalidated_by_edits"] is True
    assert {"1", "2"}.issubset(result.metadata["budget_evicted_turn_ids"])
    assert {"1", "2"}.isdisjoint(result.kept_turn_ids)


def test_claude_context_emergency_eviction_overrides_stale_protection(simple_tokenizer):
    turns = [
        Turn(
            dia_id=str(index),
            speaker="Alice",
            text=(
                "Taipei detail that is important and must be remembered "
                "with enough filler words."
            ),
            session_id="s1",
        )
        for index in range(1, 8)
    ]
    turns.append(
        Turn(
            dia_id="8",
            speaker="Bob",
            text="Current task.",
            session_id="s2",
        )
    )
    fn = make_format_and_count_fn(
        simple_tokenizer,
        "Alice",
        "Bob",
        context_format="evidence",
    )
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(
            recent_turns=1,
            retrieval_turns=0,
            max_summary_turns=2,
        )
    )

    result = compressor.compress(
        turns,
        "What important Taipei detail must be remembered?",
        64,
        fn,
    )

    assert result.token_count <= 64
    assert result.metadata["budget_satisfied"] is True
    assert result.metadata["budget_error"] is None
    assert result.kept_turn_ids[-1:] == ["8"]
    assert result.metadata["protected_budget_evicted_turn_ids"]
    assert set(result.metadata["protected_budget_evicted_turn_ids"]).issubset(
        result.metadata["protected_turn_ids"]
    )
    assert set(result.metadata["protected_budget_evicted_turn_ids"]).issubset(
        result.metadata["budget_evicted_turn_ids"]
    )


def test_claude_context_reports_unachievable_minimum_recent_budget(simple_tokenizer):
    turns = [
        Turn(
            dia_id="1",
            speaker="Alice",
            text="Old context.",
            session_id="s1",
        ),
        Turn(
            dia_id="2",
            speaker="Bob",
            text=" ".join(["current"] * 100),
            session_id="s2",
        ),
    ]
    fn = make_format_and_count_fn(
        simple_tokenizer,
        "Alice",
        "Bob",
        context_format="evidence",
    )
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(
            recent_turns=1,
            min_recent_turns=1,
            retrieval_turns=0,
        )
    )

    result = compressor.compress(turns, "What is current?", 20, fn)

    assert result.token_count > 20
    assert result.kept_turn_ids == ["2"]
    assert result.metadata["budget_satisfied"] is False
    assert (
        f"{result.token_count} > 20"
        in result.metadata["budget_error"]
    )
    assert result.metadata["budget_evicted_turn_ids"] == ["1"]


def test_claude_context_ranker_preserves_relevant_tool_result(simple_tokenizer):
    turns = [
        Turn(dia_id="1", speaker="Bob", text="[tool_result] noisy old logs stdout stderr stdout stderr.", session_id="s1"),
        Turn(dia_id="2", speaker="Bob", text="[tool_result] Taipei train leaves at 8 AM with platform details.", session_id="s1"),
        Turn(dia_id="3", speaker="Alice", text="Please answer the travel question.", session_id="s2"),
    ]
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=1, retrieval_turns=0, max_summary_turns=2)
    )

    result = compressor.compress(turns, "When does the Taipei train leave?", 220, fn)

    assert "1" in result.metadata["artifact_stub_turn_ids"]
    assert "2" not in result.metadata["artifact_stub_turn_ids"]
    assert "2" in result.kept_turn_ids
    relevant = [item for item in result.metadata["clearing_rankings"] if item["turn_id"] == "2"][0]
    assert "question_relevant" in relevant["protected_reasons"]


def test_claude_context_feature_flag_disables_tool_clearing(simple_tokenizer):
    turns = [
        Turn(dia_id="1", speaker="Bob", text="[tool_result] command output should remain raw.", session_id="s1"),
        Turn(dia_id="2", speaker="Alice", text="Current turn.", session_id="s2"),
    ]
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob", context_format="evidence")
    compressor = ClaudeContextCompressor(
        ClaudeContextPolicy(recent_turns=1, enable_tool_clearing=False, enable_compaction=False)
    )

    result = compressor.compress(turns, "What remains?", 220, fn)

    assert "clear_tool_uses_20250919" not in result.metadata["server_side_items"]
    assert result.metadata["artifact_stub_turn_ids"] == []
    assert result.kept_turn_ids == ["1", "2"]
    assert result.metadata["clearing_policy"]["enable_tool_clearing"] is False
