from __future__ import annotations

import pytest

from locomo_eval.locomo.formatter import build_chat_messages, build_evidence_messages, make_format_and_count_fn
from locomo_eval.locomo.schemas import Turn


def test_role_mapping(mock_conversation):
    messages = build_chat_messages(mock_conversation.all_turns[:2], "Alice", "Bob")
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"


def test_unknown_speaker_raises():
    with pytest.raises(ValueError):
        build_chat_messages([Turn(dia_id="x", speaker="Carol", text="hello")], "Alice", "Bob")


def test_token_count_excludes_question(mock_conversation, simple_tokenizer):
    fn = make_format_and_count_fn(simple_tokenizer, "Alice", "Bob")
    _, token_count = fn(mock_conversation.all_turns[:2], None)
    assert token_count > 0


def test_evidence_messages_include_turn_metadata(mock_conversation):
    messages = build_evidence_messages(mock_conversation.all_turns[:1], "Alice", "Bob", system_prompt="Be concise.")

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "session=s1" in messages[1]["content"]
    assert "date=2024-01-01" in messages[1]["content"]
    assert "turn=1" in messages[1]["content"]
    assert "speaker=Alice" in messages[1]["content"]
