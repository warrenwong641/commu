from __future__ import annotations

import pandas as pd

from locomo_eval.reports.attention_examples import plot_attention_examples


def test_plot_attention_examples_writes_layer_maps(tmp_path):
    results = pd.DataFrame(
        [
            {
                "conversation_id": "c1",
                "question_id": "q1",
                "method": "retrieval",
                "budget_label": "512",
                "attention_maps": {
                    "status": "ok",
                    "tokens": ["Where", "Taipei"],
                    "layers": {
                        "early": {"layer_index": 1, "attention": [[1.0, 0.0], [0.4, 0.6]]},
                        "mid": {"layer_index": 12, "attention": [[1.0, 0.0], [0.2, 0.8]]},
                        "late": {"layer_index": 23, "attention": [[1.0, 0.0], [0.1, 0.9]]},
                    },
                },
            }
        ]
    )

    outputs = plot_attention_examples(results, tmp_path)

    assert len(outputs) == 3
    assert all(path.exists() for path in outputs)
