#!/usr/bin/env python3
"""Recover the service credential in-process, then invoke one matrix cell."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from traffic_experiment.traffic_measure.cli import main as traffic_main


def process_start_ticks(pid: str) -> str:
    stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    if ") " not in stat_line:
        raise ValueError("controller stat record is malformed")
    fields = stat_line.rsplit(") ", 1)[1].split()
    if len(fields) < 20:
        raise ValueError("controller stat record is truncated")
    return fields[19]


def credential_from_controller(pid: str, expected_ticks: str) -> str:
    if not pid.isascii() or not pid.isdecimal() or int(pid) <= 1:
        raise ValueError("controller PID is invalid")
    if not expected_ticks.isascii() or not expected_ticks.isdecimal():
        raise ValueError("controller start ticks are invalid")
    if process_start_ticks(pid) != expected_ticks:
        raise ValueError("controller identity changed before credential recovery")
    with open(f"/proc/{pid}/environ", "rb", buffering=0) as source:
        entries = source.read(16 * 1024 * 1024 + 1)
    if process_start_ticks(pid) != expected_ticks:
        raise ValueError("controller identity changed during credential recovery")
    if len(entries) > 16 * 1024 * 1024:
        raise ValueError("controller environment is unexpectedly large")
    values = [
        entry.removeprefix(b"LOCAL_VLLM_API_KEY=").decode("utf-8")
        for entry in entries.split(b"\0")
        if entry.startswith(b"LOCAL_VLLM_API_KEY=")
    ]
    if (
        len(values) != 1
        or not values[0]
        or "\r" in values[0]
        or "\n" in values[0]
        or "\x00" in values[0]
    ):
        raise ValueError("controller has no unique safe API key")
    return values[0]


def main() -> int:
    if (
        len(sys.argv) < 6
        or sys.argv[1] != "--controller-pid"
        or sys.argv[3] != "--controller-start-ticks"
        or sys.argv[5] != "--"
    ):
        print(
            "usage: privileged_matrix_request.py --controller-pid PID "
            "--controller-start-ticks TICKS -- CLI_ARGS...",
            file=sys.stderr,
        )
        return 2
    try:
        credential = credential_from_controller(sys.argv[2], sys.argv[4])
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"credential recovery failed: {exc}", file=sys.stderr)
        return 2
    for name in (
        "LOCAL_VLLM_API_KEY",
        "VLLM_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
    ):
        os.environ.pop(name, None)
    sys.argv = ["traffic-measure", *sys.argv[6:], "--api-key-stdin"]
    sys.stdin = io.StringIO(credential + "\n")
    return traffic_main()


if __name__ == "__main__":
    raise SystemExit(main())
