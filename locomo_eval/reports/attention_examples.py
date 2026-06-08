from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def _as_attention_payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_name(value: Any) -> str:
    text = str(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "unknown"


def plot_attention_examples(results: pd.DataFrame, output_dir: str | Path) -> list[Path]:
    if "attention_maps" not in results.columns:
        return []

    output_path = Path(output_dir) / "attention_examples"
    output_path.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for _, row in results.iterrows():
        payload = _as_attention_payload(row.get("attention_maps"))
        if payload.get("status") != "ok":
            continue
        tokens = payload.get("tokens") or []
        layers = payload.get("layers") or {}
        if not tokens or not layers:
            continue

        for layer_name, layer_payload in layers.items():
            attention = layer_payload.get("attention") if isinstance(layer_payload, dict) else None
            if not attention:
                continue
            plt.figure(figsize=(10, 8))
            sns.heatmap(attention, xticklabels=tokens, yticklabels=tokens, cmap="viridis")
            plt.xticks(rotation=90, fontsize=6)
            plt.yticks(rotation=0, fontsize=6)
            title = (
                f"{row.get('method')} q={row.get('question_id')} "
                f"budget={row.get('budget_label')} layer={layer_name}/{layer_payload.get('layer_index')}"
            )
            plt.title(title)
            plt.tight_layout()
            file_name = "_".join(
                [
                    _safe_name(row.get("conversation_id")),
                    _safe_name(row.get("question_id")),
                    _safe_name(row.get("method")),
                    _safe_name(row.get("budget_label")),
                    _safe_name(layer_name),
                ]
            )
            target = output_path / f"{file_name}.png"
            plt.savefig(target)
            plt.close()
            written.append(target)

    return written
