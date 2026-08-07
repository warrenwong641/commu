#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command "${VLLM_BIN}"
require_value LOCAL_VLLM_API_KEY

# vLLM reads this environment variable directly. Keeping the value out of
# --api-key prevents it from appearing in process command lines.
export VLLM_API_KEY="${LOCAL_VLLM_API_KEY}"

echo "Starting ${VLLM_MODEL} on ${VLLM_HOST}:${VLLM_PORT}"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"

revision_args=()
if [[ -n "${VLLM_MODEL_REVISION:-}" ]]; then
  revision_args=(--revision "${VLLM_MODEL_REVISION}")
fi

exec "${VLLM_BIN}" serve "${VLLM_MODEL}" \
  --host "${VLLM_HOST}" \
  --port "${VLLM_PORT}" \
  --served-model-name "${VLLM_SERVED_MODEL_NAME}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --reasoning-parser qwen3 \
  --language-model-only \
  "${revision_args[@]}" \
  --generation-config vllm
