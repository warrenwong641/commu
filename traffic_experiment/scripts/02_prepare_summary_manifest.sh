#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
OUTPUT_PATH="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli prepare-summaries \
  --data-dir "${LOCOMO_DATA_DIR}" \
  --output "$(absolute_from_experiment "${OUTPUT_PATH}")" \
  --conversations "${SUMMARY_CONVERSATIONS:-10}" \
  --seed "${RANDOM_SEED}" \
  --compressor-model "${COMPRESSOR_MODEL}" \
  --compressor-device "${COMPRESSOR_DEVICE}"
