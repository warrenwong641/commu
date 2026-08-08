#!/usr/bin/env bash
set -euo pipefail

readonly UV_VERSION="0.11.33"
readonly PYTHON_VERSION="3.12.13"
readonly PYTHON_PLATFORM="x86_64-manylinux_2_28"
readonly PYPI_INDEX="https://pypi.org/simple"
readonly EXCLUDE_NEWER="2026-08-08T12:00:00Z"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly OUTPUT="${1:-requirements.lock}"
readonly UV_BIN="${UV_BIN:-uv}"
readonly RECORDED_COMMAND='CUDA_VISIBLE_DEVICES= uv --no-config pip compile --python-version 3.12.13 --python-platform x86_64-manylinux_2_28 --default-index https://pypi.org/simple --torch-backend cu129 --exclude-newer 2026-08-08T12:00:00Z --generate-hashes --emit-index-url requirements.in -o requirements.lock'

if [[ "$("${UV_BIN}" --version)" != "uv ${UV_VERSION} (x86_64-unknown-linux-gnu)" ]]; then
  echo "uv ${UV_VERSION} for x86_64 Linux is required; found: $("${UV_BIN}" --version)" >&2
  exit 1
fi

cd "${SCRIPT_DIR}"
unset UV_CONFIG_FILE UV_DEFAULT_INDEX UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL
unset UV_FIND_LINKS UV_KEYRING_PROVIDER UV_EXCLUDE_NEWER UV_TORCH_BACKEND
unset PIP_CONFIG_FILE PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS
unset PIP_TRUSTED_HOST PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX

CUDA_VISIBLE_DEVICES= UV_CUSTOM_COMPILE_COMMAND="${RECORDED_COMMAND}" \
  "${UV_BIN}" --no-config pip compile \
    --python-version "${PYTHON_VERSION}" \
    --python-platform "${PYTHON_PLATFORM}" \
    --default-index "${PYPI_INDEX}" \
    --torch-backend cu129 \
    --exclude-newer "${EXCLUDE_NEWER}" \
    --generate-hashes \
    --emit-index-url \
    requirements.in -o "${OUTPUT}"
