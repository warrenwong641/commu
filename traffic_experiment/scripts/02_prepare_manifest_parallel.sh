#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value LOCOMO_DATA_DIR
require_command sha256sum
require_command setsid
if [[ ! -d "${LOCOMO_DATA_DIR}" ]]; then
  echo "LoCoMo data directory does not exist: ${LOCOMO_DATA_DIR}" >&2
  exit 2
fi
CONDITIONS=(no_compression longllmlingua_2x longllmlingua_4x)
PREPARATION_PYTHON="$(select_manifest_python "${CONDITIONS[@]}")"
if [[ ! -x "${PREPARATION_PYTHON}" ]]; then
  echo "Preparation Python not found: ${PREPARATION_PYTHON}; run 01_setup_runner.sh first." >&2
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

for output_path in \
  "${MANIFEST_ABS}" "${MANIFEST_ABS}.tmp" \
  "${SHARD_ZERO}" "${SHARD_ZERO}.tmp" "${LOG_ZERO}" \
  "${SHARD_ONE}" "${SHARD_ONE}.tmp" "${LOG_ONE}"; do
  if [[ -e "${output_path}" || -L "${output_path}" ]]; then
    echo "Refusing to overwrite manifest preparation artifact: ${output_path}" >&2
    echo "Preserve the existing attempt and choose a new MANIFEST_PATH." >&2
    exit 2
  fi
done

prepare_shard() {
  local shard_index="$1"
  local device="$2"
  local output="$3"
  trap - INT TERM
  cd "${PYTHON_WORK_DIR}"
  export CUDA_VISIBLE_DEVICES="${device}"
  exec setsid env -u PYTHONHOME \
    PYTHONPATH="${REPOSITORY_ROOT}" \
    PYTHONSAFEPATH=1 \
    "${PREPARATION_PYTHON}" -P \
    -m traffic_experiment.traffic_measure.cli prepare \
    --data-dir "${LOCOMO_DATA_DIR}" \
    --output "${output}" \
    --samples 32 \
    --seed "${RANDOM_SEED}" \
    --compressor-model "${COMPRESSOR_MODEL}" \
    --compressor-device "${COMPRESSOR_DEVICE}" \
    --conditions "${CONDITIONS[@]}" \
    --shard-count "${SHARD_COUNT}" \
    --shard-index "${shard_index}"
}

pids=()
pid_start_ticks=()
pid_active=()

register_child() {
  local pid="$1"
  local ticks
  if ! ticks="$(record_owned_session_start_ticks "${pid}")"; then
    echo "Could not record exact identity for preparation child PID ${pid}." >&2
    cleanup_failed_session_registration "${pid}" "manifest shard"
  fi
  pids+=("${pid}")
  pid_start_ticks+=("${ticks}")
  pid_active+=(1)
}

cleanup_children_on_exit() {
  local status=$?
  local cleanup_failed=0
  local index
  trap - EXIT
  for index in "${!pids[@]}"; do
    [[ "${pid_active[index]:-0}" -eq 1 ]] || continue
    if stop_owned_child \
      "${pids[index]}" "${pid_start_ticks[index]}" "manifest shard ${index}"; then
      pid_active[index]=0
    else
      cleanup_failed=1
    fi
  done
  if [[ "${cleanup_failed}" -ne 0 && "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup_children_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

prepare_shard 0 0 "${SHARD_ZERO}" >"${LOG_ZERO}" 2>&1 &
PID_ZERO=$!
register_child "${PID_ZERO}"
prepare_shard 1 1 "${SHARD_ONE}" >"${LOG_ONE}" 2>&1 &
PID_ONE=$!
register_child "${PID_ONE}"

echo "GPU 0 shard PID: ${PID_ZERO}; log: ${LOG_ZERO}"
echo "GPU 1 shard PID: ${PID_ONE}; log: ${LOG_ONE}"

status=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[index]}"; then
    status=1
  fi
  pid_active[index]=0
done
if [[ "${status}" -ne 0 ]]; then
  echo "At least one preparation shard failed; inspect ${LOG_ZERO} and ${LOG_ONE}." >&2
  exit "${status}"
fi

(
  cd "${PYTHON_WORK_DIR}"
  run_python_safely "${RUNNER_PYTHON}" \
    -m traffic_experiment.traffic_measure.cli merge-manifests \
    --input "${SHARD_ZERO}" "${SHARD_ONE}" \
    --output "${MANIFEST_ABS}" \
    --expected-rows 96
)

chmod a-w -- "${MANIFEST_ABS}" "${SHARD_ZERO}" "${SHARD_ONE}"
echo "Parallel manifest prepared at ${MANIFEST_ABS}"
echo "MANIFEST_SHA256=$(sha256sum "${MANIFEST_ABS}" | awk '{print $1}')"
