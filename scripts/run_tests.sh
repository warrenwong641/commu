#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CORE_PYTHON="${CORE_PYTHON:-${REPOSITORY_ROOT}/.venv/bin/python}"
TRAFFIC_PYTHON="${TRAFFIC_PYTHON:-${REPOSITORY_ROOT}/traffic_experiment/.venv-runner/bin/python}"
SAFE_WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/commu-tests.XXXXXX")"
trap 'rm -rf -- "${SAFE_WORKDIR}"' EXIT

require_test_python() {
  local label="$1"
  local python_bin="$2"
  if [[ ! -x "${python_bin}" ]]; then
    echo "${label} Python not found: ${python_bin}" >&2
    exit 1
  fi
  "${python_bin}" -P -c '
import importlib.util
import sys
if sys.version_info[:2] != (3, 11):
    raise SystemExit(f"Python 3.11 is required; found {sys.version.split()[0]}")
if importlib.util.find_spec("pytest") is None:
    raise SystemExit("pytest is not installed in this environment")
'
}

require_test_python "Core" "${CORE_PYTHON}"
require_test_python "Traffic" "${TRAFFIC_PYTHON}"

cd "${SAFE_WORKDIR}"
export PYTHONPATH="${REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
# The suites use loopback mock servers and must not depend on caller proxy
# configuration or optional HTTPX proxy extras.
unset ALL_PROXY HTTP_PROXY HTTPS_PROXY all_proxy http_proxy https_proxy

"${CORE_PYTHON}" -P -m pytest -c "${REPOSITORY_ROOT}/pytest.ini" -q \
  "${REPOSITORY_ROOT}/tests"
"${TRAFFIC_PYTHON}" -P -m pytest -c "${REPOSITORY_ROOT}/pytest.ini" -q \
  "${REPOSITORY_ROOT}/traffic_experiment/tests"
