from __future__ import annotations

import pytest

from locomo_eval.locomo.formatter import build_chat_messages, make_format_and_count_fn
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
