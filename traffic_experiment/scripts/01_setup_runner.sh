#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

RUNNER_VENV="${RUNNER_VENV:-${EXPERIMENT_ROOT}/.venv-runner}"
COMPRESSION_VENV="${COMPRESSION_VENV:-${EXPERIMENT_ROOT}/.venv-compression}"
RUNNER_LOCK_FILE="${EXPERIMENT_ROOT}/requirements-runner.lock"
COMPRESSION_LOCK_FILE="${EXPERIMENT_ROOT}/requirements-compression.lock"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

require_python_311() {
  local python_bin="$1"
  "${python_bin}" -P -c '
import sys
if sys.version_info[:2] != (3, 11):
    raise SystemExit(f"Python 3.11 is required; found {sys.version.split()[0]}")
'
}

for lock_file in "${RUNNER_LOCK_FILE}" "${COMPRESSION_LOCK_FILE}"; do
  if [[ ! -f "${lock_file}" ]]; then
    echo "Missing dependency lock: ${lock_file}" >&2
    exit 1
  fi
done

sync_with_uv() {
  local venv_path="$1" lock_file="$2"
  uv venv --python 3.11 --clear "${venv_path}"
  require_python_311 "${venv_path}/bin/python"
  uv pip sync --require-hashes --python "${venv_path}/bin/python" "${lock_file}"
}

sync_with_bundled_pip() {
  local venv_path="$1" lock_file="$2"
  "${PYTHON_BIN}" -m venv --clear "${venv_path}"
  require_python_311 "${venv_path}/bin/python"
  if ! "${venv_path}/bin/python" -P -m pip install --help 2>&1 | grep -q -- '--require-hashes'; then
    echo "The pip bundled with ${PYTHON_BIN} does not support --require-hashes." >&2
    echo "Install uv, or provide a Python 3.11 build with a compatible bundled pip." >&2
    exit 1
  fi
  "${venv_path}/bin/python" -P -m pip install --require-hashes -r "${lock_file}"
}

if command -v uv >/dev/null 2>&1; then
  sync_with_uv "${RUNNER_VENV}" "${RUNNER_LOCK_FILE}"
  sync_with_uv "${COMPRESSION_VENV}" "${COMPRESSION_LOCK_FILE}"
else
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "uv is unavailable and ${PYTHON_BIN} was not found." >&2
    echo "Install uv, or install Python 3.11 and set PYTHON_BIN." >&2
    exit 1
  fi
  require_python_311 "${PYTHON_BIN}"
  sync_with_bundled_pip "${RUNNER_VENV}" "${RUNNER_LOCK_FILE}"
  sync_with_bundled_pip "${COMPRESSION_VENV}" "${COMPRESSION_LOCK_FILE}"
fi

echo
echo "Runner Python 3.11 environment is ready at ${RUNNER_VENV}."
echo "Compression Python 3.11 environment is ready at ${COMPRESSION_VENV}."
echo
echo "For vLLM, use a CUDA-compatible environment recommended for your server."
echo "The VLLM_BIN setting in server.env may point to that environment's vllm executable."
