from __future__ import annotations

import pandas as pd


def build_ablation_table(results: pd.DataFrame) -> pd.DataFrame:
    return results.groupby(["method", "budget"], dropna=False)[["token_f1", "rouge_l", "evidence_recall"]].mean().reset_index()
