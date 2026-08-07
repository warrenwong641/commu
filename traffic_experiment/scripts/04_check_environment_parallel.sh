#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
require_value CAPTURE_INTERFACE
require_value LOCAL_VLLM_API_KEY
require_command dumpcap
require_command tshark

if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  echo "Runner Python not found: ${RUNNER_PYTHON}" >&2
  exit 2
fi

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
if [[ ! -f "${MANIFEST_ABS}" ]]; then
  echo "Manifest not found: ${MANIFEST_ABS}" >&2
  exit 2
fi
if ! dumpcap -D | grep -F -- "${CAPTURE_INTERFACE}" >/dev/null; then
  echo "Capture interface '${CAPTURE_INTERFACE}' was not found by dumpcap -D." >&2
  exit 2
fi

PARALLEL_WORKERS="${PARALLEL_WORKERS:-2}"
VLLM_PORT_STEP="${VLLM_PORT_STEP:-1}"
for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port=$((VLLM_PORT + worker * VLLM_PORT_STEP))
  echo "Checking worker ${worker} on port ${port}"
  "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli check \
    --base-url "http://${VLLM_HOST}:${port}/v1"
done

echo "Parallel environment check passed."
