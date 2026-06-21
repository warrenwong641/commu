from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    return list(value) if isinstance(value, tuple) else []


def build_token_perplexity_frame(results: pd.DataFrame) -> pd.DataFrame:
    required = {"method", "question_id", "token_nlls", "answer_tokens"}
    if not required.issubset(results.columns):
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    id_cols = [col for col in ["conversation_id", "question_id", "method", "budget_label", "budget"] if col in results.columns]
    for _, row in results.iterrows():
        token_nlls = _as_list(row.get("token_nlls"))
        answer_tokens = _as_list(row.get("answer_tokens"))
        for idx, (token, nll) in enumerate(zip(answer_tokens, token_nlls)):
            nll_float = float(nll)
            payload = {col: row[col] for col in id_cols}
            payload.update(
                {
                    "token_index": idx,
                    "token": str(token),
                    "token_nll": nll_float,
                    "token_perplexity": math.exp(min(nll_float, 50.0)),
                }
            )
            rows.append(payload)
    return pd.DataFrame(rows)


def plot_token_perplexity_distribution(results: pd.DataFrame, output_dir: str | Path, top_n: int = 100) -> dict[str, Path]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    token_frame = build_token_perplexity_frame(results)
    if token_frame.empty:
        return {}

    token_csv = output_path / "answer_token_perplexity.csv"
    token_frame.to_csv(token_csv, index=False)

    high_tokens = token_frame.sort_values("token_nll", ascending=False).head(top_n)
    high_token_csv = output_path / "high_perplexity_tokens.csv"
    high_tokens.to_csv(high_token_csv, index=False)

    low_tokens = token_frame.sort_values("token_nll", ascending=True).head(top_n)
    low_token_csv = output_path / "low_perplexity_tokens.csv"
    low_tokens.to_csv(low_token_csv, index=False)

    distribution_path = output_path / "token_nll_distribution.png"
    plt.figure(figsize=(10, 6))
    sns.violinplot(data=token_frame, x="method", y="token_nll", cut=0, inner="quartile")
    plt.xticks(rotation=30, ha="right")
    plt.ylabel("Answer token NLL")
    plt.xlabel("Method")
    plt.tight_layout()
    plt.savefig(distribution_path)
    plt.close()

    budget_path = output_path / "token_nll_by_budget.png"
    if "budget_label" in token_frame.columns:
        plt.figure(figsize=(10, 6))
        sns.boxplot(data=token_frame, x="budget_label", y="token_nll", hue="method", showfliers=False)
        plt.xticks(rotation=30, ha="right")
        plt.ylabel("Answer token NLL")
        plt.xlabel("Budget")
        plt.tight_layout()
        plt.savefig(budget_path)
        plt.close()
    else:
        budget_path = distribution_path

    return {
        "token_csv": token_csv,
        "high_token_csv": high_token_csv,
        "low_token_csv": low_token_csv,
        "distribution": distribution_path,
        "budget_distribution": budget_path,
    }
