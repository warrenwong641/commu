from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .common import utc_now


_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]Inf|NaN)$"
)
_INTERESTING = re.compile(
    r"(?:token|request|queue|running|waiting|time_to_first|latency|e2e)",
    re.IGNORECASE,
)


def parse_prometheus_totals(text: str) -> dict[str, float]:
    """Aggregate Prometheus samples by metric name across label sets."""
    totals: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        value = float(match.group("value"))
        if math.isfinite(value):
            name = match.group("name")
            totals[name] = totals.get(name, 0.0) + value
    return totals


def fetch_vllm_snapshot(
    metrics_url: str,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    started = time.time()
    response = httpx.get(metrics_url, timeout=timeout_seconds)
    response.raise_for_status()
    totals = parse_prometheus_totals(response.text)
    selected = {
        name: value
        for name, value in sorted(totals.items())
        if _INTERESTING.search(name)
    }
    return {
        "schema_version": 1,
        "captured_at_utc": utc_now(),
        "captured_at_epoch_seconds": started,
        "metrics_url": metrics_url,
        "http_status": response.status_code,
        "metrics": selected,
    }


def diff_vllm_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    elapsed = float(after["captured_at_epoch_seconds"]) - float(
        before["captured_at_epoch_seconds"]
    )
    if elapsed <= 0:
        raise ValueError("after snapshot must be later than before snapshot")
    before_metrics = before.get("metrics") or {}
    after_metrics = after.get("metrics") or {}
    deltas = {
        name: float(after_metrics[name]) - float(before_metrics.get(name, 0.0))
        for name in sorted(after_metrics)
        if name.endswith(("_total", "_count", "_sum"))
        and float(after_metrics[name]) >= float(before_metrics.get(name, 0.0))
    }
    rates = {
        f"{name}_per_second": value / elapsed
        for name, value in deltas.items()
        if "token" in name.lower() or "request" in name.lower()
    }
    return {
        "schema_version": 1,
        "before_utc": before["captured_at_utc"],
        "after_utc": after["captured_at_utc"],
        "elapsed_seconds": elapsed,
        "counter_deltas": deltas,
        "counter_rates": rates,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value
