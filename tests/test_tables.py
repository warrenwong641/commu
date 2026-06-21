from __future__ import annotations

import pandas as pd

from locomo_eval.reports.tables import add_method_family


def test_oracle_is_labeled_as_upper_bound():
    frame = add_method_family(pd.DataFrame([{"method": "oracle_evidence"}, {"method": "retrieval"}]))

    assert frame.loc[0, "method_display"] == "upper_bound_oracle"
    assert frame.loc[0, "method_family"] == "upper_bound_oracle"
    assert frame.loc[1, "method_family"] == "real_method"
