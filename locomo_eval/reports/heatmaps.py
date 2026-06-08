from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from locomo_eval.reports.tables import add_method_family


def plot_attention_heatmap(results: pd.DataFrame, output_dir: str | Path) -> Path:
    results = add_method_family(results)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figure_path = output_path / "attention_heatmap.png"
    idx_col = "budget_label" if "budget_label" in results.columns else "budget"
    value_col = "evidence_recall" if "evidence_recall" in results.columns else "token_f1"
    method_col = "method_display" if "method_display" in results.columns else "method"
    pivot = results.pivot_table(index=method_col, columns=idx_col, values=value_col, aggfunc="mean")
    plt.figure(figsize=(8, 5))
    sns.heatmap(pivot, annot=True, fmt=".2f", cmap="viridis")
    plt.tight_layout()
    plt.savefig(figure_path)
    plt.close()
    return figure_path
