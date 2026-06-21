from __future__ import annotations

from pathlib import Path

import pandas as pd


def export_failure_cases(results: pd.DataFrame, output_dir: str | Path, threshold: float = 0.5) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / "failure_cases.csv"
    failures = results[results["token_f1"] < threshold]
    failures.to_csv(target, index=False)
    return target
