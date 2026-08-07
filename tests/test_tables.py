from __future__ import annotations

import pandas as pd

from locomo_eval.reports.tables import add_method_family, build_ablation_table


def test_oracle_is_labeled_as_upper_bound():
    frame = add_method_family(pd.DataFrame([{"method": "oracle_evidence"}, {"method": "retrieval"}]))

    assert frame.loc[0, "method_display"] == "upper_bound_oracle"
    assert frame.loc[0, "method_family"] == "upper_bound_oracle"
    assert frame.loc[1, "method_family"] == "real_method"


def test_invalid_budget_rows_are_excluded_from_analysis_tables():
    results = pd.DataFrame(
        [
            {
                "method": "claude_context",
                "budget_label": "64",
                "token_f1": 1.0,
                "valid_for_analysis": True,
            },
            {
                "method": "claude_context",
                "budget_label": "64",
                "token_f1": None,
                "valid_for_analysis": False,
            },
        ]
    )

    table = build_ablation_table(results)

    assert len(table) == 1
    assert table.loc[0, "n"] == 1
    assert table.loc[0, "token_f1"] == 1.0


def test_string_false_validity_is_not_treated_as_truthy():
    frame = add_method_family(
        pd.DataFrame(
            [
                {"method": "retrieval", "valid_for_analysis": "True"},
                {"method": "claude_context", "valid_for_analysis": "False"},
            ]
        )
    )

    assert frame["method"].tolist() == ["retrieval"]
