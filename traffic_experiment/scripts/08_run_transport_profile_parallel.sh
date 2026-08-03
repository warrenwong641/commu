#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap
require_command tshark

PARALLEL_WORKERS="${PARALLEL_WORKERS:-2}"
if [[ "${PARALLEL_WORKERS}" -ne 2 ]]; then
  echo "The secure proxy currently defines exactly two isolated workers." >&2
  exit 2
fi

TRANSPORT="${TRANSPORT:-tls13}"
case "${TRANSPORT}" in
  tls13)
    PORTS=(8443 8543)
    FILTER_PROTOCOL=tcp
    require_command curl
    ;;
  http3)
    PORTS=(8444 8544)
    FILTER_PROTOCOL=udp
    if ! "${RUNNER_PYTHON}" -c 'import aioquic' >/dev/null 2>&1; then
      echo "aioquic is absent; rerun 01_setup_runner.sh." >&2
      exit 2
    fi
    ;;
  *)
    echo "TRANSPORT must be tls13 or http3." >&2
    exit 2
    ;;
esac

case "${PROFILE}" in
  pilot) SAMPLES=8; REPETITIONS=3 ;;
  main) SAMPLES=32; REPETITIONS=5 ;;
  robustness) SAMPLES=32; REPETITIONS=10 ;;
  *) echo "PROFILE must be pilot, main, or robustness." >&2; exit 2 ;;
esac

CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
CA_FILE="${CADDY_RUN_DIR}/data/caddy/pki/authorities/local/root.crt"
if [[ ! -f "${CA_FILE}" ]]; then
  echo "Missing ${CA_FILE}; run 07_start_secure_proxy.sh first." >&2
  exit 2
fi

MANIFEST_PATH_EFFECTIVE="${MANIFEST_PATH_OVERRIDE:-${MANIFEST_PATH}}"
RUNS_ROOT_EFFECTIVE="${RUNS_ROOT_OVERRIDE:-${RUNS_ROOT}}"
MAX_OUTPUT_TOKENS_EFFECTIVE="${MAX_OUTPUT_TOKENS_OVERRIDE:-${MAX_OUTPUT_TOKENS}}"
OBSERVATION_SECONDS_EFFECTIVE="${OBSERVATION_SECONDS_OVERRIDE:-${OBSERVATION_SECONDS}}"
MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH_EFFECTIVE}")"
RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT_EFFECTIVE}")/local_vllm_${TRANSPORT}_${PROFILE}"
mkdir -p "${RUN_DIR}"

pids=()
for worker in 0 1; do
  port="${PORTS[$worker]}"
  worker_dir="${RUN_DIR}/worker-${worker}"
  worker_log="${RUN_DIR}/worker-${worker}.log"
  echo "Worker ${worker}: secure port=${port}, output=${worker_dir}"
  "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
    --manifest "${MANIFEST_ABS}" \
    --output-dir "${worker_dir}" \
    --backend local_vllm \
    --base-url "https://localhost:${port}/v1" \
    --model "${VLLM_SERVED_MODEL_NAME}" \
    --api-key "${LOCAL_VLLM_API_KEY}" \
    --samples "${SAMPLES}" \
    --repetitions "${REPETITIONS}" \
    --seed "${RANDOM_SEED}" \
    --max-output-tokens "${MAX_OUTPUT_TOKENS_EFFECTIVE}" \
    --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
    --observation-seconds "${OBSERVATION_SECONDS_EFFECTIVE}" \
    --capture-interface "${CAPTURE_INTERFACE}" \
    --capture-filter "${FILTER_PROTOCOL} port ${port}" \
    --worker-count 2 \
    --worker-index "${worker}" \
    --transport "${TRANSPORT}" \
    --connection-mode cold \
    --tls-ca-file "${CA_FILE}" \
    >"${worker_log}" 2>&1 &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  wait "${pid}" || failed=1
done
if [[ "${failed}" -ne 0 ]]; then
  echo "A worker failed; inspect ${RUN_DIR}/worker-*.log." >&2
  exit 1
fi

RESULTS="${RUN_DIR}/results.jsonl"
"${RUNNER_PYTHON}" - "${RUN_DIR}" "${RESULTS}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
rows = []
seen = set()
for worker in range(2):
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
rows.sort(key=lambda row: (
    str(row["sample_id"]),
    str(row["condition"]),
    int(row["repetition"]),
))
output.write_text(
    "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
    encoding="utf-8",
)
print(f"Merged {len(rows)} disjoint worker results into {output}")
PY

(
  cd /tmp
  "${RUNNER_PYTHON}" -P -m traffic_experiment.traffic_measure.cli analyze \
    --results "${RESULTS}" \
    --output "${RUN_DIR}/traffic_metrics.csv"
)
echo "Parallel ${TRANSPORT} profile complete: ${RESULTS}"
