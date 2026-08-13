#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command "${VLLM_BIN}"
require_value LOCAL_VLLM_API_KEY
require_command setsid
require_command ss

# vLLM reads this environment variable directly. Keeping the value out of
# --api-key prevents it from appearing in worker command lines.
export VLLM_API_KEY="${LOCAL_VLLM_API_KEY}"

load_worker_topology
PARALLEL_WORKERS="${TOPOLOGY_WORKER_COUNT}"
LOG_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/vllm"
mkdir -p "${LOG_DIR}"

pids=()
pid_start_ticks=()
pid_active=()
worker_ports=()
revision_args=()
if [[ -n "${VLLM_MODEL_REVISION:-}" ]]; then
  revision_args=(--revision "${VLLM_MODEL_REVISION}")
fi

worker_ports=("${WORKER_VLLM_PORTS[@]}")

register_vllm_child() {
  local pid="$1" ticks
  if ! ticks="$(record_owned_session_start_ticks "${pid}")"; then
    echo "Could not record exact identity for vLLM child PID ${pid}." >&2
    cleanup_failed_session_registration "${pid}" "vLLM worker"
  fi
  pids+=("${pid}")
  pid_start_ticks+=("${ticks}")
  pid_active+=(1)
}

vllm_listeners_closed() {
  local tcp_listeners port
  if ! command -v ss >/dev/null 2>&1; then
    echo "Cannot verify vLLM listener closure because ss is unavailable." >&2
    return 1
  fi
  if ! tcp_listeners="$(ss -H -ltn 2>/dev/null)"; then
    echo "Failed to inspect TCP listeners after stopping vLLM." >&2
    return 1
  fi
  for port in "${worker_ports[@]}"; do
    if grep -Eq ":${port}[[:space:]]" <<<"${tcp_listeners}"; then
      echo "A TCP listener remains on expected vLLM port ${port}." >&2
      return 1
    fi
  done
}

cleanup_vllm_on_exit() {
  local status=$?
  local cleanup_failed=0
  local index
  trap - EXIT
  for index in "${!pids[@]}"; do
    [[ "${pid_active[index]:-0}" -eq 1 ]] || continue
    if stop_owned_child \
      "${pids[index]}" "${pid_start_ticks[index]}" "vLLM worker ${index}"; then
      pid_active[index]=0
    else
      cleanup_failed=1
    fi
  done
  if ! vllm_listeners_closed; then
    cleanup_failed=1
  fi
  if [[ "${cleanup_failed}" -ne 0 && "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup_vllm_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port="${worker_ports[worker]}"
  log="${LOG_DIR}/gpu-${worker}-port-${port}.log"
  gpu_id="${WORKER_GPU_IDS[$worker]}"
  echo "Starting worker ${worker}: physical GPU ${gpu_id}, port ${port}, log ${log}"
  (
    trap - INT TERM
    export CUDA_VISIBLE_DEVICES="${gpu_id}"
    exec setsid "${VLLM_BIN}" serve "${VLLM_MODEL}" \
      --host "${VLLM_HOST}" \
      --port "${port}" \
      --served-model-name "${VLLM_SERVED_MODEL_NAME}" \
      --tensor-parallel-size 1 \
      --max-model-len "${MAX_MODEL_LEN}" \
      --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
      --language-model-only \
      "${revision_args[@]}" \
      --generation-config vllm
  ) >"${log}" 2>&1 &
  register_vllm_child "$!"
done

echo "vLLM worker PIDs: ${pids[*]}"
exited_pid=""
set +e
wait -n -p exited_pid "${pids[@]}"
wait_status=$?
set -e

for index in "${!pids[@]}"; do
  if [[ "${pids[index]}" == "${exited_pid}" ]] &&
    ! owned_session_group_has_live_members "${pids[index]}"; then
    pid_active[index]=0
    break
  fi
done

if [[ "${wait_status}" -eq 0 ]]; then
  wait_status=1
fi
echo "vLLM worker PID ${exited_pid:-unknown} exited; stopping the remaining owned workers." >&2
exit "${wait_status}"
