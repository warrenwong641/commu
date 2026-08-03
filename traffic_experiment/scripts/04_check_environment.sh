#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
require_value CAPTURE_INTERFACE
require_command dumpcap
require_command tshark

if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  echo "Runner Python not found: ${RUNNER_PYTHON}" >&2
  exit 2
fi

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
if [[ ! -f "${MANIFEST_ABS}" ]]; then
  echo "Manifest not found: ${MANIFEST_ABS}; run 02_prepare_manifest.sh." >&2
  exit 2
fi

if ! dumpcap -D | grep -F -- "${CAPTURE_INTERFACE}" >/dev/null; then
  echo "Capture interface '${CAPTURE_INTERFACE}' was not found by dumpcap -D." >&2
  exit 2
fi

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli check \
  --base-url "http://${VLLM_HOST}:${VLLM_PORT}/v1" \
  --api-key "${LOCAL_VLLM_API_KEY}"

echo "Environment check passed."
