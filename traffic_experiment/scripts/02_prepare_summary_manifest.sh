#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
require_command sha256sum
OUTPUT_PATH="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"
OUTPUT_ABS="$(absolute_from_experiment "${OUTPUT_PATH}")"
if [[ -e "${OUTPUT_ABS}" || -L "${OUTPUT_ABS}" ||
  -e "${OUTPUT_ABS}.tmp" || -L "${OUTPUT_ABS}.tmp" ]]; then
  echo "Refusing to overwrite frozen summary manifest output: ${OUTPUT_ABS}" >&2
  echo "Preserve the existing artifact and choose a new SUMMARY_MANIFEST_PATH." >&2
  exit 2
fi
read -r -a SUMMARY_CONDITION_ARGS <<< \
  "${SUMMARY_CONDITIONS:-no_compression longllmlingua_2x longllmlingua_4x}"
require_compressor_device_for_conditions \
  "${COMPRESSOR_DEVICE}" "${SUMMARY_CONDITION_ARGS[@]}"
PREPARATION_PYTHON="$(select_manifest_python "${SUMMARY_CONDITION_ARGS[@]}")"
if [[ ! -x "${PREPARATION_PYTHON}" ]]; then
  echo "Preparation Python not found: ${PREPARATION_PYTHON}; run 01_setup_runner.sh first." >&2
  exit 2
fi

run_python_safely "${PREPARATION_PYTHON}" \
  -m traffic_experiment.traffic_measure.cli prepare-summaries \
    --data-dir "${LOCOMO_DATA_DIR}" \
    --output "${OUTPUT_ABS}" \
    --conversations "${SUMMARY_CONVERSATIONS:-10}" \
    --seed "${RANDOM_SEED}" \
    --compressor-model "${COMPRESSOR_MODEL}" \
    --compressor-device "${COMPRESSOR_DEVICE}" \
  --conditions "${SUMMARY_CONDITION_ARGS[@]}"

chmod a-w -- "${OUTPUT_ABS}"
echo "Summary manifest prepared at ${OUTPUT_ABS}"
echo "SUMMARY_MANIFEST_SHA256=$(sha256sum "${OUTPUT_ABS}" | awk '{print $1}')"
