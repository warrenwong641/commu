#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
UV_VERSION="0.11.33"
PYTHON_RESOLUTION_VERSION="3.11.15"
TARGET_PLATFORM="x86_64-unknown-linux-gnu"
DEFAULT_INDEX="https://pypi.org/simple"
EXCLUDE_NEWER="2026-08-08T00:00:00Z"

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "Lock generation is supported only on Linux x86_64." >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv ${UV_VERSION} is required to regenerate the locks." >&2
  exit 1
fi
actual_uv_version="$(uv --version | awk '{print $2}')"
if [[ "${actual_uv_version}" != "${UV_VERSION}" ]]; then
  echo "uv ${UV_VERSION} is required; found ${actual_uv_version}." >&2
  exit 1
fi
resolver_python="$(uv --no-config python find "${PYTHON_RESOLUTION_VERSION}")"
resolver_identity="$("${resolver_python}" -P -c '
import platform
import sys
print(f"{platform.python_implementation()} {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
')"
if [[ "${resolver_identity}" != "CPython ${PYTHON_RESOLUTION_VERSION}" ]]; then
  echo "CPython ${PYTHON_RESOLUTION_VERSION} is required; found ${resolver_identity}." >&2
  exit 1
fi

# Prevent user or CI package-index configuration from changing the resolution.
unset UV_INDEX UV_DEFAULT_INDEX UV_EXTRA_INDEX_URL UV_FIND_LINKS UV_NO_INDEX
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_NO_INDEX

compile_lock() {
  local output_file="$1"
  shift
  uv --no-config pip compile \
    --python "${resolver_python}" \
    --python-version "${PYTHON_RESOLUTION_VERSION}" \
    --python-platform "${TARGET_PLATFORM}" \
    --default-index "${DEFAULT_INDEX}" \
    --emit-index-url \
    --generate-hashes \
    --exclude-newer "${EXCLUDE_NEWER}" \
    --custom-compile-command "bash scripts/compile_python_locks.sh (uv ${UV_VERSION}; CPython ${PYTHON_RESOLUTION_VERSION}; ${TARGET_PLATFORM}; ${DEFAULT_INDEX}; exclude-newer ${EXCLUDE_NEWER})" \
    "$@" \
    -o "${output_file}"
}

cd "${REPOSITORY_ROOT}"
compile_lock requirements.lock requirements.txt requirements-test.txt
compile_lock traffic_experiment/requirements-runner.lock \
  traffic_experiment/requirements-runner.txt \
  traffic_experiment/requirements-test.txt

echo "Regenerated hashed Linux x86_64 locks with uv ${UV_VERSION}."
