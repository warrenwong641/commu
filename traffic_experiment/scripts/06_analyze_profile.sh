#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command tshark

RUNS_ABS="$(absolute_from_experiment "${RUNS_ROOT}")"
RUN_DIR="${RUNS_ABS}/local_vllm_${PROFILE}"
RESULTS="${RUN_DIR}/results.jsonl"
OUTPUT="${RUN_DIR}/traffic_metrics.csv"

if [[ ! -f "${RESULTS}" ]]; then
  echo "Results not found: ${RESULTS}" >&2
  exit 2
fi

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli analyze \
  --results "${RESULTS}" \
  --output "${OUTPUT}"

echo "Traffic metrics: ${OUTPUT}"
