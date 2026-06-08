from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def save_results(rows: list[dict], output_dir: str | Path, file_name: str = "results.parquet") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    target = output_path / file_name
    frame.to_parquet(target, index=False)
    return target


def load_completed(output_dir: str | Path, file_name: str = "results.parquet") -> set[tuple[str, str, str, Any]]:
    target = Path(output_dir) / file_name
    if not target.exists():
        return set()
    existing = pd.read_parquet(target)
    if existing.empty:
        return set()
    required = {"conversation_id", "question_id", "method", "budget"}
    if not required.issubset(existing.columns):
        return set()
    completed: set[tuple[str, str, str, Any]] = set()
    for _, row in existing.iterrows():
        completed.add((str(row["conversation_id"]), str(row["question_id"]), str(row["method"]), row["budget"]))
    return completed


def save_checkpoint(state: dict, output_dir: str | Path, file_name: str = "checkpoint.json") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / file_name
    with target.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    return target
