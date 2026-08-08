#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

RUNNER_VENV="${RUNNER_VENV:-${EXPERIMENT_ROOT}/.venv-runner}"
LOCK_FILE="${EXPERIMENT_ROOT}/requirements-runner.lock"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

require_python_311() {
  local python_bin="$1"
  "${python_bin}" -P -c '
import sys
if sys.version_info[:2] != (3, 11):
    raise SystemExit(f"Python 3.11 is required; found {sys.version.split()[0]}")
'
}

if [[ ! -f "${LOCK_FILE}" ]]; then
  echo "Missing dependency lock: ${LOCK_FILE}" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.11 --clear "${RUNNER_VENV}"
  require_python_311 "${RUNNER_VENV}/bin/python"
  uv pip sync --require-hashes --python "${RUNNER_VENV}/bin/python" "${LOCK_FILE}"
else
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "uv is unavailable and ${PYTHON_BIN} was not found." >&2
    echo "Install uv, or install Python 3.11 and set PYTHON_BIN." >&2
    exit 1
  fi
  require_python_311 "${PYTHON_BIN}"
  "${PYTHON_BIN}" -m venv --clear "${RUNNER_VENV}"
  "${RUNNER_VENV}/bin/python" -P -m pip install --upgrade pip
  "${RUNNER_VENV}/bin/python" -P -m pip install --require-hashes -r "${LOCK_FILE}"
fi

echo
echo "Runner Python 3.11 environment is ready at ${RUNNER_VENV}."
echo "For LongLLMLingua preparation, also run:"
echo "  uv pip install --python ${RUNNER_VENV}/bin/python -r ${EXPERIMENT_ROOT}/requirements-compression.txt"
echo "Without uv, use:"
echo "  ${RUNNER_VENV}/bin/python -P -m pip install -r ${EXPERIMENT_ROOT}/requirements-compression.txt"
echo
echo "For vLLM, use a CUDA-compatible environment recommended for your server."
echo "The VLLM_BIN setting in server.env may point to that environment's vllm executable."
