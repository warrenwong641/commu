#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command "${VLLM_BIN}"

echo "Starting ${VLLM_MODEL} on ${VLLM_HOST}:${VLLM_PORT}"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"

exec "${VLLM_BIN}" serve "${VLLM_MODEL}" \
  --host "${VLLM_HOST}" \
  --port "${VLLM_PORT}" \
  --served-model-name "${VLLM_SERVED_MODEL_NAME}" \
  --api-key "${LOCAL_VLLM_API_KEY}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --generation-config vllm
