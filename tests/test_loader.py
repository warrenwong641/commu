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


def test_loader_parses_locomo_github_format(tmp_path):
    raw = [
        {
            "sample_id": "conv-26",
            "conversation": {
                "speaker_a": "Caroline",
                "speaker_b": "Melanie",
                "session_1": [
                    {"speaker": "Caroline", "dia_id": "D1:1", "text": "I moved to Taipei."},
                    {"speaker": "Melanie", "dia_id": "D1:2", "text": "You moved to Taipei."},
                ],
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_2": [
                    {"speaker": "Caroline", "dia_id": "D2:1", "text": "I like night markets."},
                ],
                "session_2_date_time": "2:00 pm on 9 May, 2023",
            },
            "qa": [
                {
                    "question": "Where did Caroline move?",
                    "answer": "Taipei",
                    "evidence": ["D1:1"],
                    "category": 2,
                }
            ],
            "session_summary": {
                "session_1_summary": "Caroline moved to Taipei.",
                "session_2_summary": "Caroline likes night markets.",
            },
            "event_summary": {
                "session_1_summary": "Move discussed.",
                "session_2_summary": "Preference discussed.",
            },
            "observation": {
                "session_1": ["Move event"],
                "session_2": {"detail": "Food preference"},
            },
        }
    ]
    (tmp_path / "locomo10.json").write_text(json.dumps(raw), encoding="utf-8")

    conversations = load_conversations(tmp_path)

    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.conversation_id == "conv-26"
    assert conversation.speaker_a == "Caroline"
    assert conversation.speaker_b == "Melanie"
    assert len(conversation.sessions) == 2
    assert conversation.sessions[0].session_id == "s1"
    assert conversation.sessions[0].timestamp == "1:56 pm on 8 May, 2023"
    assert conversation.sessions[0].summary == "Caroline moved to Taipei."
    assert conversation.sessions[1].event_summary == "Preference discussed."
    assert conversation.sessions[1].observations == ["Food preference"]
    assert conversation.qa_examples[0].evidence_ids == ["D1:1"]
