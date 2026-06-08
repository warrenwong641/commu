from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def save_results(rows: list[dict], output_dir: str | Path, file_name: str = "results.parquet") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    target = output_path / file_name
    frame.to_parquet(target, index=False)
    return target


def save_checkpoint(state: dict, output_dir: str | Path, file_name: str = "checkpoint.json") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / file_name
    with target.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    return target
