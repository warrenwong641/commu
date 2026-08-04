#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap

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
for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port=$((VLLM_PORT + worker * VLLM_PORT_STEP))
  worker_dir="${RUN_DIR}/worker-${worker}"
  worker_log="${RUN_DIR}/worker-${worker}.log"
  echo "Worker ${worker}: port=${port}, output=${worker_dir}, log=${worker_log}"
  "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
    --manifest "${MANIFEST_ABS}" \
    --output-dir "${worker_dir}" \
    --base-url "http://${VLLM_HOST}:${port}/v1" \
    --model "${VLLM_SERVED_MODEL_NAME}" \
    --api-key "${LOCAL_VLLM_API_KEY}" \
    --samples "${SAMPLES}" \
    --repetitions "${REPETITIONS}" \
    --seed "${RANDOM_SEED}" \
    --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
    --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
    --observation-seconds "${OBSERVATION_SECONDS}" \
    --capture-interface "${CAPTURE_INTERFACE}" \
    --capture-filter "tcp port ${port}" \
    --worker-count "${PARALLEL_WORKERS}" \
    --worker-index "${worker}" \
    >"${worker_log}" 2>&1 &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  wait "${pid}" || failed=1
done
if [[ "${failed}" -ne 0 ]]; then
  echo "At least one measurement worker failed; inspect ${RUN_DIR}/worker-*.log." >&2
  exit 1
fi

RESULTS="${RUN_DIR}/results.jsonl"
"${RUNNER_PYTHON}" - "${RUN_DIR}" "${RESULTS}" "${PARALLEL_WORKERS}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
worker_count = int(sys.argv[3])
rows = []
seen = set()
for worker in range(worker_count):
    path = run_dir / f"worker-{worker}" / "results.jsonl"
    if not path.exists():
        raise SystemExit(f"missing worker results: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (row["request_id"], int(row["repetition"]))
        if key in seen:
            raise SystemExit(f"duplicate trial across workers: {key}")
        seen.add(key)
        rows.append(row)
rows.sort(key=lambda row: (str(row["sample_id"]), str(row["condition"]), int(row["repetition"])))
output.write_text(
    "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
    encoding="utf-8",
)
print(f"Merged {len(rows)} disjoint worker results into {output}")
PY

echo "Parallel profile complete: ${RESULTS}"
