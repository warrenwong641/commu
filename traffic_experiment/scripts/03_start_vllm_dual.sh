#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command "${VLLM_BIN}"

PARALLEL_WORKERS="${PARALLEL_WORKERS:-2}"
VLLM_PORT_STEP="${VLLM_PORT_STEP:-1}"
IFS=',' read -r -a GPU_IDS <<<"${CUDA_VISIBLE_DEVICES:-0,1}"
if ((${#GPU_IDS[@]} < PARALLEL_WORKERS)); then
  echo "CUDA_VISIBLE_DEVICES must list at least ${PARALLEL_WORKERS} GPU IDs." >&2
  exit 2
fi
LOG_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/vllm"
mkdir -p "${LOG_DIR}"

pids=()
revision_args=()
if [[ -n "${VLLM_MODEL_REVISION:-}" ]]; then
  revision_args=(--revision "${VLLM_MODEL_REVISION}")
fi
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port=$((VLLM_PORT + worker * VLLM_PORT_STEP))
  log="${LOG_DIR}/gpu-${worker}-port-${port}.log"
  gpu_id="${GPU_IDS[$worker]}"
  echo "Starting worker ${worker}: physical GPU ${gpu_id}, port ${port}, log ${log}"
  CUDA_VISIBLE_DEVICES="${gpu_id}" "${VLLM_BIN}" serve "${VLLM_MODEL}" \
    --host "${VLLM_HOST}" \
    --port "${port}" \
    --served-model-name "${VLLM_SERVED_MODEL_NAME}" \
    --api-key "${LOCAL_VLLM_API_KEY}" \
    --tensor-parallel-size 1 \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    "${revision_args[@]}" \
    --generation-config vllm \
    >"${log}" 2>&1 &
  pids+=("$!")
done

echo "vLLM worker PIDs: ${pids[*]}"
wait "${pids[@]}"
