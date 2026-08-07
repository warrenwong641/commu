#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
require_command sha256sum
if [[ ! -d "${LOCOMO_DATA_DIR}" ]]; then
  echo "LoCoMo data directory does not exist: ${LOCOMO_DATA_DIR}" >&2
  exit 2
fi
if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  echo "Runner Python not found: ${RUNNER_PYTHON}; run 01_setup_runner.sh first." >&2
  exit 2
fi

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
if [[ -e "${MANIFEST_ABS}" || -L "${MANIFEST_ABS}" ||
  -e "${MANIFEST_ABS}.tmp" || -L "${MANIFEST_ABS}.tmp" ]]; then
  echo "Refusing to overwrite frozen manifest output: ${MANIFEST_ABS}" >&2
  echo "Preserve the existing artifact and choose a new MANIFEST_PATH." >&2
  exit 2
fi
mkdir -p "$(dirname -- "${MANIFEST_ABS}")"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli prepare \
  --data-dir "${LOCOMO_DATA_DIR}" \
  --output "${MANIFEST_ABS}" \
  --samples 32 \
  --seed "${RANDOM_SEED}" \
  --compressor-model "${COMPRESSOR_MODEL}" \
  --compressor-device "${COMPRESSOR_DEVICE}" \
  --conditions no_compression longllmlingua_2x longllmlingua_4x

chmod a-w -- "${MANIFEST_ABS}"
echo "Manifest prepared at ${MANIFEST_ABS}"
echo "MANIFEST_SHA256=$(sha256sum "${MANIFEST_ABS}" | awk '{print $1}')"
echo "Stop the compressor process/environment before starting vLLM to release GPU memory."
