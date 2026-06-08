from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def plot_attention_heatmap(results: pd.DataFrame, output_dir: str | Path) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figure_path = output_path / "attention_heatmap.png"
    pivot = results.pivot_table(index="method", columns="budget", values="evidence_recall", aggfunc="mean")
    plt.figure(figsize=(8, 5))
    sns.heatmap(pivot, annot=True, fmt=".2f", cmap="viridis")
    plt.tight_layout()
    plt.savefig(figure_path)
    plt.close()
    return figure_path
