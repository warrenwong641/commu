#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap

case "${PROFILE}" in
  pilot)
    SAMPLES=8
    REPETITIONS=3
    ;;
  main)
    SAMPLES=32
    REPETITIONS=5
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

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
RUNS_ABS="$(absolute_from_experiment "${RUNS_ROOT}")"
RUN_DIR="${RUNS_ABS}/local_vllm_${PROFILE}"
mkdir -p "${RUN_DIR}"

echo "Profile: ${PROFILE}; samples=${SAMPLES}; repetitions=${REPETITIONS}"
echo "Output: ${RUN_DIR}"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST_ABS}" \
  --output-dir "${RUN_DIR}" \
  --base-url "http://${VLLM_HOST}:${VLLM_PORT}/v1" \
  --model "${VLLM_SERVED_MODEL_NAME}" \
  --api-key "${LOCAL_VLLM_API_KEY}" \
  --samples "${SAMPLES}" \
  --repetitions "${REPETITIONS}" \
  --seed "${RANDOM_SEED}" \
  --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
  --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
  --observation-seconds "${OBSERVATION_SECONDS}" \
  --capture-interface "${CAPTURE_INTERFACE}" \
  --capture-filter "${CAPTURE_FILTER}"
