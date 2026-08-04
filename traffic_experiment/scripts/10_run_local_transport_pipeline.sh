#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TLS_RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/local_vllm_tls13_pilot"
TLS_PID_FILE="${TLS_RUN_DIR}/launcher.pid"

validate_run() {
  local run_dir="$1"
  local expected_rows="$2"
  "${RUNNER_PYTHON}" - "${run_dir}/results.jsonl" "${expected_rows}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = int(sys.argv[2])
if not path.exists():
    raise SystemExit(f"missing results: {path}")
rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
if len(rows) != expected:
    raise SystemExit(f"{path}: expected {expected} rows, found {len(rows)}")
bad = [
    row["run_id"]
    for row in rows
    if not row.get("completed") or not row.get("capture_sha256")
]
if bad:
    raise SystemExit(f"{path}: {len(bad)} incomplete rows")
print(f"Validated {len(rows)} complete captured rows: {path}")
PY
}

if [[ -f "${TLS_PID_FILE}" ]]; then
  tls_pid="$(cat "${TLS_PID_FILE}")"
  while kill -0 "${tls_pid}" 2>/dev/null; do
    sleep 30
  done
fi
validate_run "${TLS_RUN_DIR}" 72
echo "TLS_QA_OK"

TRANSPORT=http3 PROFILE=pilot \
  "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
validate_run "$(absolute_from_experiment "${RUNS_ROOT}")/local_vllm_http3_pilot" 72
echo "HTTP3_QA_OK"

summary_runs_root="${SUMMARY_RUNS_ROOT:-runs/event_summary}"
summary_manifest="${SUMMARY_BASELINE_MANIFEST_PATH:-artifacts/event_summaries_no_compression.jsonl}"

TRANSPORT=tls13 PROFILE=pilot \
RUNS_ROOT_OVERRIDE="${summary_runs_root}" \
MANIFEST_PATH_OVERRIDE="${summary_manifest}" \
MAX_OUTPUT_TOKENS_OVERRIDE="${SUMMARY_MAX_OUTPUT_TOKENS:-1024}" \
OBSERVATION_SECONDS_OVERRIDE="${SUMMARY_OBSERVATION_SECONDS:-60}" \
  "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
validate_run \
  "$(absolute_from_experiment "${summary_runs_root}")/local_vllm_tls13_pilot" \
  24
echo "TLS_SUMMARY_OK"

TRANSPORT=http3 PROFILE=pilot \
RUNS_ROOT_OVERRIDE="${summary_runs_root}" \
MANIFEST_PATH_OVERRIDE="${summary_manifest}" \
MAX_OUTPUT_TOKENS_OVERRIDE="${SUMMARY_MAX_OUTPUT_TOKENS:-1024}" \
OBSERVATION_SECONDS_OVERRIDE="${SUMMARY_OBSERVATION_SECONDS:-60}" \
  "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
validate_run \
  "$(absolute_from_experiment "${summary_runs_root}")/local_vllm_http3_pilot" \
  24
echo "HTTP3_SUMMARY_OK"
