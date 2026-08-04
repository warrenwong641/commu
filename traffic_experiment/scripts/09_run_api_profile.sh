#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap
BACKEND="${BACKEND:-openrouter}"
case "${BACKEND}" in
  openrouter)
    require_value OPENROUTER_API_KEY
    require_value OPENROUTER_MODEL
    require_value OPENROUTER_PROVIDER
    BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
    MODEL="${OPENROUTER_MODEL}"
    PROVIDER_ARGS=(--openrouter-provider "${OPENROUTER_PROVIDER}")
    ;;
  gemini)
    require_value GEMINI_API_KEY
    require_value GEMINI_MODEL
    BASE_URL="${GEMINI_BASE_URL:-https://generativelanguage.googleapis.com/v1beta}"
    MODEL="${GEMINI_MODEL}"
    PROVIDER_ARGS=()
    ;;
  *)
    echo "BACKEND must be openrouter or gemini." >&2
    exit 2
    ;;
esac

case "${PROFILE}" in
  pilot) SAMPLES=8; REPETITIONS=3 ;;
  main) SAMPLES=32; REPETITIONS="${MAIN_REPETITIONS:-3}" ;;
  robustness) SAMPLES=32; REPETITIONS=10 ;;
  *) echo "PROFILE must be pilot, main, or robustness." >&2; exit 2 ;;
esac
SAMPLES="${SAMPLES_OVERRIDE:-${SAMPLES}}"
REPETITIONS="${REPETITIONS_OVERRIDE:-${REPETITIONS}}"

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/${BACKEND}_${PROFILE}"
mkdir -p "${RUN_DIR}"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST_ABS}" \
  --output-dir "${RUN_DIR}" \
  --backend "${BACKEND}" \
  --base-url "${BASE_URL}" \
  --model "${MODEL}" \
  --samples "${SAMPLES}" \
  --repetitions "${REPETITIONS}" \
  --seed "${RANDOM_SEED}" \
  --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
  --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
  --observation-seconds "${OBSERVATION_SECONDS}" \
  --capture-interface "${CAPTURE_INTERFACE}" \
  --capture-filter "tcp port 443" \
  --transport http1 \
  --connection-mode warm \
  "${PROVIDER_ARGS[@]}"
