#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_value LOCAL_VLLM_API_KEY
require_command dumpcap
require_command setsid

PARALLEL_WORKERS="${PARALLEL_WORKERS:-2}"
VLLM_PORT_STEP="${VLLM_PORT_STEP:-1}"

case "${PROFILE}" in
  pilot)
    SAMPLES=8
    REPETITIONS=3
    ;;
  main)
    SAMPLES=32
    REPETITIONS="${MAIN_REPETITIONS:-3}"
    ;;
  robustness)
    SAMPLES=32
    REPETITIONS=10
    ;;
  *)
    echo "PROFILE must be pilot, main, or robustness; got '${PROFILE}'." >&2
    exit 2
    ;;
esac
SAMPLES="${SAMPLES_OVERRIDE:-${SAMPLES}}"
REPETITIONS="${REPETITIONS_OVERRIDE:-${REPETITIONS}}"

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
RUNS_ABS="$(absolute_from_experiment "${RUNS_ROOT}")"
RUN_DIR="${RUNS_ABS}/local_vllm_${PROFILE}"
mkdir -p "${RUN_DIR}"

pids=()
pid_start_ticks=()
pid_active=()

register_child() {
  local pid="$1"
  local ticks
  if ! ticks="$(record_owned_session_start_ticks "${pid}")"; then
    echo "Could not record exact identity for measurement child PID ${pid}." >&2
    cleanup_failed_session_registration "${pid}" "measurement worker"
  fi
  pids+=("${pid}")
  pid_start_ticks+=("${ticks}")
  pid_active+=(1)
}

cleanup_children_on_exit() {
  local status=$?
  local cleanup_failed=0
  local index
  trap - EXIT
  for index in "${!pids[@]}"; do
    [[ "${pid_active[index]:-0}" -eq 1 ]] || continue
    if stop_owned_child \
      "${pids[index]}" "${pid_start_ticks[index]}" "measurement worker ${index}"; then
      pid_active[index]=0
    else
      cleanup_failed=1
    fi
  done
  if [[ "${cleanup_failed}" -ne 0 && "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup_children_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port=$((VLLM_PORT + worker * VLLM_PORT_STEP))
  worker_dir="${RUN_DIR}/worker-${worker}"
  worker_log="${RUN_DIR}/worker-${worker}.log"
  echo "Worker ${worker}: port=${port}, output=${worker_dir}, log=${worker_log}"
  (
    trap - INT TERM
    exec setsid "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
      --manifest "${MANIFEST_ABS}" \
      --output-dir "${worker_dir}" \
      --base-url "http://${VLLM_HOST}:${port}/v1" \
      --model "${VLLM_SERVED_MODEL_NAME}" \
      --samples "${SAMPLES}" \
      --repetitions "${REPETITIONS}" \
      --seed "${RANDOM_SEED}" \
      --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
      --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
      --observation-seconds "${OBSERVATION_SECONDS}" \
      --capture-interface "${CAPTURE_INTERFACE}" \
      --capture-filter "tcp port ${port}" \
      --worker-count "${PARALLEL_WORKERS}" \
      --worker-index "${worker}"
  ) >"${worker_log}" 2>&1 &
  register_child "$!"
done

failed=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[index]}"; then
    failed=1
  fi
  pid_active[index]=0
done
if [[ "${failed}" -ne 0 ]]; then
  echo "At least one measurement worker failed; inspect ${RUN_DIR}/worker-*.log." >&2
  exit 1
fi

RESULTS="${RUN_DIR}/results.jsonl"
"${RUNNER_PYTHON}" - "${RUN_DIR}" "${RESULTS}" "${PARALLEL_WORKERS}" <<'PY'
import json
import fcntl
import hashlib
import os
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
worker_count = int(sys.argv[3])


def canonical(row):
    return json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def attempt_identity(row, encoded):
    attempt_id = row.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return ("attempt_id", attempt_id)
    return ("legacy_sha256", hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def register(row, source, seen_attempts, assignments):
    encoded = canonical(row)
    identity = attempt_identity(row, encoded)
    previous = seen_attempts.get(identity)
    if previous is not None:
        if previous != encoded:
            raise SystemExit(f"conflicting duplicate attempt {identity} in {source}")
        return False
    trial = (str(row["request_id"]), int(row["repetition"]))
    worker = int(row["worker_index"])
    assigned = assignments.setdefault(trial, worker)
    if assigned != worker:
        raise SystemExit(f"trial assigned to multiple workers: {trial}")
    seen_attempts[identity] = encoded
    return True


output.parent.mkdir(parents=True, exist_ok=True)
with output.open("a+", encoding="utf-8", newline="\n") as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    stream.seek(0)
    seen_attempts = {}
    assignments = {}
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        register(
            json.loads(line),
            f"{output}:{line_number}",
            seen_attempts,
            assignments,
        )

    new_rows = []
    for worker in range(worker_count):
        path = run_dir / f"worker-{worker}" / "results.jsonl"
        if not path.exists():
            raise SystemExit(f"missing worker results: {path}")
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row["worker_index"]) != worker:
                raise SystemExit(
                    f"{path}:{line_number}: worker_index does not match shard"
                )
            if register(
                row,
                f"{path}:{line_number}",
                seen_attempts,
                assignments,
            ):
                new_rows.append(row)

    new_rows.sort(
        key=lambda row: (
            str(row["sample_id"]),
            str(row["condition"]),
            int(row["repetition"]),
            int(row.get("attempt", 0)),
            str(row.get("attempt_id", "")),
        )
    )
    stream.seek(0, os.SEEK_END)
    for row in new_rows:
        stream.write(canonical(row) + "\n")
    stream.flush()
    os.fsync(stream.fileno())

print(
    f"Appended {len(new_rows)} new attempts to {output}; "
    f"{len(seen_attempts)} attempts recorded"
)
PY

echo "Parallel profile complete: ${RESULTS}"
