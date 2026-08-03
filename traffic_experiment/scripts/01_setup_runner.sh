#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
"${PYTHON_BIN}" -m venv "${EXPERIMENT_ROOT}/.venv-runner"
"${EXPERIMENT_ROOT}/.venv-runner/bin/python" -m pip install --upgrade pip
"${EXPERIMENT_ROOT}/.venv-runner/bin/python" -m pip install \
  -r "${EXPERIMENT_ROOT}/requirements-runner.txt"

echo
echo "Runner environment is ready."
echo "For LongLLMLingua preparation, also run:"
echo "  ${EXPERIMENT_ROOT}/.venv-runner/bin/python -m pip install -r ${EXPERIMENT_ROOT}/requirements-compression.txt"
echo
echo "For vLLM, use a CUDA-compatible environment recommended for your server."
echo "The VLLM_BIN setting in server.env may point to that environment's vllm executable."
