#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
if [[ ! -d "${LOCOMO_DATA_DIR}" ]]; then
  echo "LoCoMo data directory does not exist: ${LOCOMO_DATA_DIR}" >&2
  exit 2
fi
if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  echo "Runner Python not found: ${RUNNER_PYTHON}; run 01_setup_runner.sh first." >&2
  exit 2
fi

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
mkdir -p "$(dirname -- "${MANIFEST_ABS}")"

SHARD_COUNT=2
SHARD_ZERO="${MANIFEST_ABS%.jsonl}.shard-0-of-${SHARD_COUNT}.jsonl"
SHARD_ONE="${MANIFEST_ABS%.jsonl}.shard-1-of-${SHARD_COUNT}.jsonl"
LOG_ZERO="${SHARD_ZERO%.jsonl}.log"
LOG_ONE="${SHARD_ONE%.jsonl}.log"
PYTHON_WORK_DIR="${TMPDIR:-/tmp}"

prepare_shard() {
  local shard_index="$1"
  local device="$2"
  local output="$3"
  local log="$4"
  (
    cd "${PYTHON_WORK_DIR}"
    CUDA_VISIBLE_DEVICES="${device}" "${RUNNER_PYTHON}" \
      -m traffic_experiment.traffic_measure.cli prepare \
      --data-dir "${LOCOMO_DATA_DIR}" \
      --output "${output}" \
      --samples 32 \
      --seed "${RANDOM_SEED}" \
      --compressor-model "${COMPRESSOR_MODEL}" \
      --compressor-device "${COMPRESSOR_DEVICE}" \
      --conditions no_compression longllmlingua_2x longllmlingua_4x \
      --shard-count "${SHARD_COUNT}" \
      --shard-index "${shard_index}"
  ) >"${log}" 2>&1
}

prepare_shard 0 0 "${SHARD_ZERO}" "${LOG_ZERO}" &
PID_ZERO=$!
prepare_shard 1 1 "${SHARD_ONE}" "${LOG_ONE}" &
PID_ONE=$!

echo "GPU 0 shard PID: ${PID_ZERO}; log: ${LOG_ZERO}"
echo "GPU 1 shard PID: ${PID_ONE}; log: ${LOG_ONE}"

status=0
wait "${PID_ZERO}" || status=$?
wait "${PID_ONE}" || status=$?
if [[ "${status}" -ne 0 ]]; then
  echo "At least one preparation shard failed; inspect ${LOG_ZERO} and ${LOG_ONE}." >&2
  exit "${status}"
fi

(
  cd "${PYTHON_WORK_DIR}"
  "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli merge-manifests \
    --input "${SHARD_ZERO}" "${SHARD_ONE}" \
    --output "${MANIFEST_ABS}" \
    --expected-rows 96
)

echo "Parallel manifest prepared at ${MANIFEST_ABS}"
