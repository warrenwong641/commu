from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def plot_budget_vs_performance(results: pd.DataFrame, output_dir: str | Path) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figure_path = output_path / "budget_vs_performance.png"
    plt.figure(figsize=(8, 5))
    sns.lineplot(data=results, x="budget", y="token_f1", hue="method", marker="o")
    plt.tight_layout()
    plt.savefig(figure_path)
    plt.close()
    return figure_path
