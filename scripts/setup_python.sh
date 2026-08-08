#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${REPOSITORY_ROOT}/.venv}"
LOCK_FILE="${REPOSITORY_ROOT}/requirements.lock"
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
  uv venv --python 3.11 --clear "${VENV_PATH}"
  require_python_311 "${VENV_PATH}/bin/python"
  uv pip sync --require-hashes --python "${VENV_PATH}/bin/python" "${LOCK_FILE}"
else
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "uv is unavailable and ${PYTHON_BIN} was not found." >&2
    echo "Install uv, or install Python 3.11 and set PYTHON_BIN." >&2
    exit 1
  fi
  require_python_311 "${PYTHON_BIN}"
  "${PYTHON_BIN}" -m venv --clear "${VENV_PATH}"
  "${VENV_PATH}/bin/python" -P -m pip install --upgrade pip
  "${VENV_PATH}/bin/python" -P -m pip install --require-hashes -r "${LOCK_FILE}"
fi

echo "Core Python 3.11 environment is ready at ${VENV_PATH}."
echo "Run tests with: bash ${REPOSITORY_ROOT}/scripts/run_tests.sh"
