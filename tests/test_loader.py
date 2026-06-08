from __future__ import annotations

import json

from locomo_eval.locomo.loader import load_conversations
from locomo_eval.locomo.qa_builder import build_qa_examples


def test_loader_filters_category_five(tmp_path, mock_conversation):
    raw = {
        "conversation_id": mock_conversation.conversation_id,
        "speaker_a": mock_conversation.speaker_a,
        "speaker_b": mock_conversation.speaker_b,
        "sessions": [
            {
                "session_id": session.session_id,
                "timestamp": session.timestamp,
                "session_summary": session.summary,
                "turns": [{"dia_id": turn.dia_id, "speaker": turn.speaker, "text": turn.text} for turn in session.turns],
            }
            for session in mock_conversation.sessions
        ],
        "qa": [
            {
                "question_id": qa.question_id,
                "question": qa.question,
                "answer": qa.answer,
                "evidence": qa.evidence_ids,
                "category": qa.category,
            }
            for qa in mock_conversation.qa_examples
        ],
    }
    (tmp_path / "sample.json").write_text(json.dumps(raw), encoding="utf-8")
    conversations = load_conversations(tmp_path)
    examples = build_qa_examples(conversations)
    assert len(conversations) == 1
    assert len(examples) == 2
