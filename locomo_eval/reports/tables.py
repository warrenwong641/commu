from __future__ import annotations

import numpy as np
import pandas as pd


def add_method_family(results: pd.DataFrame) -> pd.DataFrame:
    frame = results.copy()
    if "valid_for_analysis" in frame.columns:
        def is_valid(value) -> bool:
            if pd.isna(value):
                return True
            if isinstance(value, str):
                return value.strip().lower() not in {
                    "false",
                    "0",
                    "no",
                    "invalid",
                }
            return bool(value)

        frame = frame.loc[
            frame["valid_for_analysis"].map(is_valid)
        ].copy()
    if "method" not in frame.columns:
        return frame
    frame["method_family"] = frame["method"].apply(lambda method: "upper_bound_oracle" if method == "oracle_evidence" else "real_method")
    frame["method_display"] = frame["method"].replace({"oracle_evidence": "upper_bound_oracle"})
    return frame


def _bootstrap_ci(values: pd.Series, n_bootstrap: int = 1000, seed: int = 42) -> tuple[float | None, float | None]:
    clean = values.dropna().astype(float).to_numpy()
    if clean.size == 0:
        return None, None
    if clean.size == 1:
        value = float(clean[0])
        return value, value
    rng = np.random.default_rng(seed)
    samples = rng.choice(clean, size=(n_bootstrap, clean.size), replace=True).mean(axis=1)
    low, high = np.percentile(samples, [2.5, 97.5])
    return float(low), float(high)


def build_ablation_table(results: pd.DataFrame) -> pd.DataFrame:
    results = add_method_family(results)
    group_col = "budget_label" if "budget_label" in results.columns else "budget"
    method_col = "method_display" if "method_display" in results.columns else "method"
    metric_cols = ["token_f1", "rouge_l", "exact_match", "date_f1", "number_f1", "entity_f1", "llm_judge_score", "evidence_recall", "evidence_session_recall", "answerable_context_rate", "perplexity", "latency"]
    available = [c for c in metric_cols if c in results.columns]
    rows = []
    for group_key, group in results.groupby([method_col, "method_family", group_col], dropna=False):
        row = {"method": group_key[0], "method_family": group_key[1], group_col: group_key[2], "n": int(len(group))}
        for metric in available:
            row[metric] = group[metric].mean()
            low, high = _bootstrap_ci(group[metric])
            row[f"{metric}_ci_low"] = low
            row[f"{metric}_ci_high"] = high
        rows.append(row)
    return pd.DataFrame(rows)


def build_category_table(results: pd.DataFrame) -> pd.DataFrame:
    results = add_method_family(results)
    if "category" not in results.columns:
        return pd.DataFrame()
    group_col = "budget_label" if "budget_label" in results.columns else "budget"
    method_col = "method_display" if "method_display" in results.columns else "method"
    metric_cols = ["token_f1", "rouge_l", "exact_match", "date_f1", "number_f1", "entity_f1", "llm_judge_score", "evidence_recall", "evidence_session_recall", "answerable_context_rate"]
    available = [c for c in metric_cols if c in results.columns]
    return results.groupby(["category", method_col, "method_family", group_col], dropna=False)[available].mean().reset_index()
