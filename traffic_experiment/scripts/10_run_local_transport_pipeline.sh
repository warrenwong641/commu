#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TLS_RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/local_vllm_tls13_pilot"
TLS_PID_FILE="${TLS_RUN_DIR}/launcher.pid"
TLS_LAUNCHER_STATE="${TLS_RUN_DIR}/launcher.state"
TRANSPORT_LAUNCHER_SCRIPT="$(
  readlink -f -- "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
)"

launcher_state_value() {
  local key="$1"
  awk -F= -v key="${key}" \
    '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${TLS_LAUNCHER_STATE}"
}

launcher_pid_matches() {
  local pid="$1" expected_ticks="$2"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/${pid}/cmdline" || return 1
  local argument
  for argument in "${argv[@]}"; do
    if [[ "$(readlink -f -- "${argument}" 2>/dev/null || true)" == \
      "${TRANSPORT_LAUNCHER_SCRIPT}" ]]; then
      return 0
    fi
  done
  return 1
}

wait_for_recorded_tls_launcher() {
  if [[ -e "${TLS_PID_FILE}" || -L "${TLS_PID_FILE}" ]]; then
    echo "Refusing legacy bare PID file ${TLS_PID_FILE}; use launcher.state identity metadata." >&2
    return 2
  fi
  [[ -e "${TLS_LAUNCHER_STATE}" || -L "${TLS_LAUNCHER_STATE}" ]] || return 0
  if [[ -L "${TLS_LAUNCHER_STATE}" ]]; then
    echo "Refusing symlinked launcher state ${TLS_LAUNCHER_STATE}." >&2
    return 2
  fi
  local owner tls_pid tls_ticks tls_script
  owner="$(launcher_state_value owner)"
  tls_pid="$(launcher_state_value pid)"
  tls_ticks="$(launcher_state_value start_ticks)"
  tls_script="$(launcher_state_value script)"
  if [[ "${owner}" != "commu-transport-launcher-v1" ||
    ! "${tls_pid}" =~ ^[0-9]+$ ||
    ! "${tls_ticks}" =~ ^[0-9]+$ ||
    "$(readlink -f -- "${tls_script}" 2>/dev/null || true)" != \
      "${TRANSPORT_LAUNCHER_SCRIPT}" ]]; then
    echo "Refusing malformed/unrecognized TLS launcher state." >&2
    return 2
  fi
  while kill -0 "${tls_pid}" 2>/dev/null; do
    if [[ "$(process_state "${tls_pid}")" == "Z" ]]; then
      if [[ "$(process_start_ticks "${tls_pid}")" != "${tls_ticks}" ]]; then
        echo "Refusing to wait: zombie TLS launcher identity changed." >&2
        return 2
      fi
      break
    fi
    if ! launcher_pid_matches "${tls_pid}" "${tls_ticks}"; then
      echo "Refusing to wait: TLS launcher PID ${tls_pid} identity changed." >&2
      return 2
    fi
    sleep 5
  done
}

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
trials = {}
assignments = {}
for row in rows:
    key = (str(row["request_id"]), int(row["repetition"]))
    worker = int(row["worker_index"])
    previous = assignments.setdefault(key, worker)
    if previous != worker:
        raise SystemExit(f"{path}: trial assigned to multiple workers: {key}")
    trials.setdefault(key, []).append(row)
if len(trials) != expected:
    raise SystemExit(
        f"{path}: expected {expected} logical trials, found {len(trials)}"
    )
incomplete = [
    key
    for key, attempts in trials.items()
    if not any(
        row.get("completed") is True and row.get("capture_sha256")
        for row in attempts
    )
]
if incomplete:
    raise SystemExit(
        f"{path}: {len(incomplete)} logical trials lack a complete captured attempt"
    )
print(
    f"Validated {len(trials)} logical trials across {len(rows)} attempts: {path}"
)
PY
}

wait_for_recorded_tls_launcher
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
