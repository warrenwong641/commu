from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from locomo_eval.reports.tables import add_method_family


def plot_budget_vs_performance(results: pd.DataFrame, output_dir: str | Path) -> Path:
    results = add_method_family(results)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    x_col = "budget_label" if "budget_label" in results.columns else "budget"
    y_cols = [c for c in ["token_f1", "exact_match", "llm_judge_score", "evidence_recall", "evidence_session_recall", "answerable_context_rate"] if c in results.columns]
    if not y_cols:
        return output_path
    figure_path = output_path / "budget_vs_performance.png"
    fig, axes = plt.subplots(1, len(y_cols), figsize=(6 * len(y_cols), 5))
    if len(y_cols) == 1:
        axes = [axes]
    for ax, y_col in zip(axes, y_cols):
        sns.lineplot(data=results, x=x_col, y=y_col, hue="method_display", style="method_family", marker="o", ax=ax)
        ax.set_title(y_col)
    plt.tight_layout()
    plt.savefig(figure_path)
    plt.close()
    return figure_path
