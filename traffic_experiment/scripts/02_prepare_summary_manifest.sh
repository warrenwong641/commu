#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
OUTPUT_PATH="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"
read -r -a SUMMARY_CONDITION_ARGS <<< \
  "${SUMMARY_CONDITIONS:-no_compression longllmlingua_2x longllmlingua_4x}"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli prepare-summaries \
  --data-dir "${LOCOMO_DATA_DIR}" \
  --output "$(absolute_from_experiment "${OUTPUT_PATH}")" \
  --conversations "${SUMMARY_CONVERSATIONS:-10}" \
  --seed "${RANDOM_SEED}" \
  --compressor-model "${COMPRESSOR_MODEL}" \
  --compressor-device "${COMPRESSOR_DEVICE}" \
  --conditions "${SUMMARY_CONDITION_ARGS[@]}"
