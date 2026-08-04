#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
OUTPUT_PATH="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"
DEFAULT_COMPRESSION_PYTHON="$(absolute_from_experiment ".venv-compression/bin/python")"
if [[ -x "${DEFAULT_COMPRESSION_PYTHON}" ]]; then
  COMPRESSION_PYTHON="${COMPRESSION_PYTHON:-${DEFAULT_COMPRESSION_PYTHON}}"
else
  COMPRESSION_PYTHON="${COMPRESSION_PYTHON:-${RUNNER_PYTHON}}"
fi
if [[ ! -x "${COMPRESSION_PYTHON}" ]]; then
  echo "Compression Python not found: ${COMPRESSION_PYTHON}" >&2
  exit 2
fi
read -r -a SUMMARY_CONDITION_ARGS <<< \
  "${SUMMARY_CONDITIONS:-no_compression longllmlingua_2x longllmlingua_4x}"

(
  cd "${TMPDIR:-/tmp}"
  PYTHONSAFEPATH=1 "${COMPRESSION_PYTHON}" -P \
    -m traffic_experiment.traffic_measure.cli prepare-summaries \
    --data-dir "${LOCOMO_DATA_DIR}" \
    --output "$(absolute_from_experiment "${OUTPUT_PATH}")" \
    --conversations "${SUMMARY_CONVERSATIONS:-10}" \
    --seed "${RANDOM_SEED}" \
    --compressor-model "${COMPRESSOR_MODEL}" \
    --compressor-device "${COMPRESSOR_DEVICE}" \
    --conditions "${SUMMARY_CONDITION_ARGS[@]}"
)
