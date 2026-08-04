#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

PRELIM_ROOT_ABS="$(absolute_from_experiment "${PRELIM_RUNS_ROOT:-runs/preliminary}")"
QA_SAMPLES="${PRELIM_QA_SAMPLES:-16}"
SUMMARY_SAMPLES="${PRELIM_SUMMARY_SAMPLES:-8}"
REPETITIONS="${PRELIM_REPETITIONS:-1}"
EXPECTED=$(((QA_SAMPLES + SUMMARY_SAMPLES) * 3 * 2 * 2 * REPETITIONS))

"${RUNNER_PYTHON}" - "${PRELIM_ROOT_ABS}" "${EXPECTED}" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
expected = int(sys.argv[2])
latest = {}
for path in root.glob("**/worker-*/results.jsonl"):
    parts = path.parts
    network = next((value for value in ("baseline", "rtt") if value in parts), "unknown")
    workload = next((value for value in ("qa", "summary") if value in parts), "unknown")
    transport = next(
        (value for value in ("tls13", "http3") if f"local_vllm_{value}_" in str(path)),
        "unknown",
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (network, workload, transport, row["request_id"], int(row["repetition"]))
        if row.get("completed") or key not in latest:
            latest[key] = row

complete = [key for key, row in latest.items() if row.get("completed")]
failed = [key for key, row in latest.items() if not row.get("completed")]
counts = Counter(key[:3] for key in complete)
print(f"completed={len(complete)}/{expected} failed_or_incomplete={len(failed)}")
for key in sorted(counts):
    print(f"{'/'.join(key)}={counts[key]}")
PY

if [[ -f "${PRELIM_ROOT_ABS}/launcher.pid" ]]; then
  launcher_pid="$(cat "${PRELIM_ROOT_ABS}/launcher.pid")"
  if kill -0 "${launcher_pid}" 2>/dev/null; then
    echo "launcher_pid=${launcher_pid} status=running"
  else
    echo "launcher_pid=${launcher_pid} status=stopped"
  fi
fi

if [[ -f "${PRELIM_ROOT_ABS}/launcher.log" ]]; then
  echo "--- latest launcher output ---"
  tail -n 12 "${PRELIM_ROOT_ABS}/launcher.log"
fi
