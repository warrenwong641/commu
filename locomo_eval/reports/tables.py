from __future__ import annotations

import pandas as pd


def build_ablation_table(results: pd.DataFrame) -> pd.DataFrame:
    group_col = "budget_label" if "budget_label" in results.columns else "budget"
    metric_cols = ["token_f1", "rouge_l", "evidence_recall", "perplexity", "latency"]
    available = [c for c in metric_cols if c in results.columns]
    return results.groupby(["method", group_col], dropna=False)[available].mean().reset_index()
