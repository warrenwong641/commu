#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"

ENV_FILE="${EXPERIMENT_ENV_FILE:-${EXPERIMENT_ROOT}/server.env}"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  echo "Copy server.env.example to server.env and fill the required blanks." >&2
  exit 2
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

RUNNER_PYTHON="${RUNNER_PYTHON:-${EXPERIMENT_ROOT}/.venv-runner/bin/python}"
export PYTHONPATH="${REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export LOCAL_VLLM_API_KEY
export CUDA_VISIBLE_DEVICES
if [[ -n "${HF_TOKEN:-}" ]]; then
  export HF_TOKEN
fi

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Required value ${name} is blank in ${ENV_FILE}" >&2
    exit 2
  fi
}

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command not found: ${command_name}" >&2
    exit 2
  fi
}

absolute_from_experiment() {
  local path="$1"
  if [[ "${path}" = /* ]]; then
    printf '%s\n' "${path}"
  else
    printf '%s\n' "${EXPERIMENT_ROOT}/${path}"
  fi
}
