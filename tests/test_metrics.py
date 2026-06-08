from __future__ import annotations

from locomo_eval.metrics.answer_metrics import token_f1
from locomo_eval.metrics.compression_metrics import compression_ratio
from locomo_eval.metrics.evidence_metrics import evidence_recall


def test_identical_token_f1():
    assert token_f1("hello world", "hello world") == 1.0


def test_empty_context_recall():
    assert evidence_recall([], ["1"]) == 0.0


def test_compression_ratio_range():
    value = compression_ratio(100, 25)
    assert 0.0 <= value <= 1.0
