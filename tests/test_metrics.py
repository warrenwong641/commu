from __future__ import annotations

from locomo_eval.metrics.answer_metrics import legacy_token_f1, token_f1
from locomo_eval.metrics.answer_metrics import score_answer
from locomo_eval.metrics.compression_metrics import compression_ratio
from locomo_eval.metrics.evidence_metrics import answerable_context_rate, evidence_recall, evidence_session_recall


def test_identical_token_f1():
    assert token_f1("hello world", "hello world") == 1.0


def test_token_f1_normalizes_punctuation_and_articles_but_keeps_legacy_score():
    assert token_f1("The answer is Luna.", "answer is Luna") == 1.0
    assert legacy_token_f1("Luna.", "Luna") == 0.0


def test_empty_context_recall():
    assert evidence_recall([], ["1"]) == 0.0


def test_compression_ratio_range():
    value = compression_ratio(100, 25)
    assert 0.0 <= value <= 1.0


def test_date_number_entity_metrics():
    scores = score_answer("Caroline went on 7 May 2023.", "7 May 2023")
    assert scores["date_f1"] == 1.0


def test_session_recall_and_answerable_context(mock_conversation):
    kept = mock_conversation.all_turns[4:6]
    assert evidence_session_recall(kept, mock_conversation.all_turns, ["7"]) == 1.0
    assert answerable_context_rate(mock_conversation.all_turns, "Taipei") == 1.0
