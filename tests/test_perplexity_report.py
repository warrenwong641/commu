from __future__ import annotations

import pandas as pd

from locomo_eval.reports.perplexity import build_token_perplexity_frame, plot_token_perplexity_distribution


def test_build_token_perplexity_frame_explodes_answer_tokens():
    results = pd.DataFrame(
        [
            {
                "conversation_id": "c1",
                "question_id": "q1",
                "method": "retrieval",
                "budget_label": "128",
                "budget": 128,
                "token_nlls": [0.5, 2.0],
                "answer_tokens": ["Tai", "pei"],
            }
        ]
    )

    frame = build_token_perplexity_frame(results)

    assert list(frame["token"]) == ["Tai", "pei"]
    assert list(frame["token_index"]) == [0, 1]
    assert list(frame["token_nll"]) == [0.5, 2.0]


def test_plot_token_perplexity_distribution_writes_outputs(tmp_path):
    results = pd.DataFrame(
        [
            {
                "conversation_id": "c1",
                "question_id": "q1",
                "method": "retrieval",
                "budget_label": "128",
                "budget": 128,
                "token_nlls": [0.5, 2.0],
                "answer_tokens": ["Tai", "pei"],
            }
        ]
    )

    outputs = plot_token_perplexity_distribution(results, tmp_path)

    assert outputs["token_csv"].exists()
    assert outputs["high_token_csv"].exists()
    assert outputs["low_token_csv"].exists()
    assert outputs["distribution"].exists()
