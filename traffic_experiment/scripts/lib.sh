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

# Configuration is intentionally non-secret. Refuse legacy/populated files
# before sourcing them so credentials cannot be normalized into this workflow.
if grep -Eq \
  '^[[:space:]]*(export[[:space:]]+)?(LOCAL_VLLM_API_KEY|VLLM_API_KEY|OPENROUTER_API_KEY|GEMINI_API_KEY|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)[[:space:]]*=' \
  "${ENV_FILE}"; then
  echo "Credential assignment found in ${ENV_FILE}; refusing to source it." >&2
  echo "Remove the assignment and export credentials only in the invoking process." >&2
  exit 2
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

RUNNER_PYTHON="${RUNNER_PYTHON:-${EXPERIMENT_ROOT}/.venv-runner/bin/python}"
COMPRESSION_PYTHON="${COMPRESSION_PYTHON:-${EXPERIMENT_ROOT}/.venv-compression/bin/python}"
if [[ -n "${VLLM_BIN:-}" ]]; then
  VLLM_BIN_DIR="$(cd -- "$(dirname -- "${VLLM_BIN}")" && pwd)"
  export PATH="${VLLM_BIN_DIR}:${PATH}"
fi
export PYTHONPATH="${REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
if [[ -x "${EXPERIMENT_ROOT}/.tools/caddy" ]]; then
  export PATH="${EXPERIMENT_ROOT}/.tools:${PATH}"
fi
export LOCAL_VLLM_API_KEY
export CUDA_VISIBLE_DEVICES
if [[ -n "${HF_TOKEN:-}" ]]; then
  export HF_TOKEN
fi

select_manifest_python() {
  local condition
  for condition in "$@"; do
    if [[ "${condition}" != "no_compression" ]]; then
      printf '%s\n' "${COMPRESSION_PYTHON}"
      return 0
    fi
  done
  printf '%s\n' "${RUNNER_PYTHON}"
}

run_python_safely() {
  local python_bin="$1"
  shift
  (
    cd "${TMPDIR:-/tmp}"
    env -u PYTHONHOME \
      PYTHONPATH="${REPOSITORY_ROOT}" \
      PYTHONSAFEPATH=1 \
      "${python_bin}" -P "$@"
  )
}

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Required value ${name} is blank after loading ${ENV_FILE} and the current process environment." >&2
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

process_stat_tail() {
  local pid="$1" stat
  [[ "${pid}" =~ ^[0-9]+$ && -r "/proc/${pid}/stat" ]] || return 1
  IFS= read -r stat <"/proc/${pid}/stat" || return 1
  [[ "${stat}" == *") "* ]] || return 1
  printf '%s\n' "${stat##*) }"
}

process_stat_tail_field() {
  local pid="$1" field="$2" tail
  tail="$(process_stat_tail "${pid}" 2>/dev/null)" || return 0
  awk -v field="${field}" '{print $field}' <<<"${tail}"
}

process_state() {
  # Field 3 in /proc/PID/stat is field 1 after the parenthesized command.
  process_stat_tail_field "$1" 1
}

process_parent_pid() {
  process_stat_tail_field "$1" 2
}

process_group_id() {
  process_stat_tail_field "$1" 3
}

process_session_id() {
  process_stat_tail_field "$1" 4
}

process_start_ticks() {
  # Field 22 in /proc/PID/stat is field 20 after the command.
  process_stat_tail_field "$1" 20
}

owned_child_pid_matches() {
  local pid="$1" expected_ticks="$2"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  [[ "$(process_parent_pid "${pid}")" == "$$" ]] || return 1
  [[ "$(process_group_id "${pid}")" == "${pid}" ]] || return 1
  [[ "$(process_session_id "${pid}")" == "${pid}" ]]
}

record_owned_session_start_ticks() {
  local pid="$1" attempt ticks
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  for ((attempt = 0; attempt < 100; attempt++)); do
    ticks="$(process_start_ticks "${pid}")"
    if [[ -n "${ticks}" ]] &&
      owned_child_pid_matches "${pid}" "${ticks}" &&
      [[ "$(process_state "${pid}")" != "Z" ]]; then
      printf '%s\n' "${ticks}"
      return 0
    fi
    if ! kill -0 "${pid}" 2>/dev/null ||
      [[ "$(process_state "${pid}")" == "Z" ]]; then
      return 1
    fi
    sleep 0.01
  done
  return 1
}

owned_session_group_has_live_members() {
  local session_leader="$1" stat_path tail
  [[ "${session_leader}" =~ ^[0-9]+$ ]] || return 1
  for stat_path in /proc/[0-9]*/stat; do
    [[ -r "${stat_path}" ]] || continue
    IFS= read -r tail <"${stat_path}" || continue
    [[ "${tail}" == *") "* ]] || continue
    tail="${tail##*) }"
    # tail fields: state=1, process-group=3, session=4.
    set -- ${tail}
    if [[ "${1:-}" != "Z" &&
      "${3:-}" == "${session_leader}" &&
      "${4:-}" == "${session_leader}" ]]; then
      return 0
    fi
  done
  return 1
}

signal_owned_session_group() {
  local signal_name="$1" pid="$2" expected_ticks="$3" label="$4"
  if kill -0 "${pid}" 2>/dev/null &&
    [[ "$(process_state "${pid}")" != "Z" ]] &&
    ! owned_child_pid_matches "${pid}" "${expected_ticks}"; then
    echo "Refusing to signal PID ${pid}: ${label} session leader changed." >&2
    return 1
  fi
  if ! owned_child_pid_matches "${pid}" "${expected_ticks}" &&
    ! owned_session_group_has_live_members "${pid}"; then
    return 0
  fi
  if ! kill -"${signal_name}" -- "-${pid}" 2>/dev/null; then
    if owned_session_group_has_live_members "${pid}"; then
      echo "Failed to signal owned ${label} process group ${pid}." >&2
      return 1
    fi
  fi
}

wait_for_owned_child_exit() {
  local pid="$1" expected_ticks="$2" attempts="$3" attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! owned_session_group_has_live_members "${pid}"; then
      return 0
    fi
    # If the leader is still live, a changed identity means the group is no
    # longer safe to act on.
    if kill -0 "${pid}" 2>/dev/null &&
      [[ "$(process_state "${pid}")" != "Z" ]] &&
      ! owned_child_pid_matches "${pid}" "${expected_ticks}"; then
      echo "Refusing to wait on changed process-group leader PID ${pid}." >&2
      return 2
    fi
    sleep 0.1
  done
  return 1
}

stop_owned_child() {
  local pid="$1" expected_ticks="$2" label="${3:-child}"
  if kill -0 "${pid}" 2>/dev/null &&
    [[ "$(process_state "${pid}")" != "Z" ]] &&
    ! owned_child_pid_matches "${pid}" "${expected_ticks}"; then
    echo "Refusing to signal PID ${pid}: ${label} identity/session no longer matches." >&2
    return 1
  fi
  if ! owned_child_pid_matches "${pid}" "${expected_ticks}" &&
    ! owned_session_group_has_live_members "${pid}"; then
    wait "${pid}" 2>/dev/null || true
    return 0
  fi

  signal_owned_session_group INT "${pid}" "${expected_ticks}" "${label}" || return 1
  if ! wait_for_owned_child_exit "${pid}" "${expected_ticks}" 100; then
    signal_owned_session_group TERM "${pid}" "${expected_ticks}" "${label}" || return 1
    if ! wait_for_owned_child_exit "${pid}" "${expected_ticks}" 50; then
      signal_owned_session_group KILL "${pid}" "${expected_ticks}" "${label}" || return 1
      if ! wait_for_owned_child_exit "${pid}" "${expected_ticks}" 20; then
        echo "Owned ${label} process group ${pid} did not exit." >&2
        return 1
      fi
    fi
  fi
  wait "${pid}" 2>/dev/null || true
}

cleanup_failed_session_registration() {
  local pid="$1" label="${2:-child}" ticks
  ticks="$(process_start_ticks "${pid}")"
  if [[ -n "${ticks}" ]] && owned_child_pid_matches "${pid}" "${ticks}"; then
    stop_owned_child "${pid}" "${ticks}" "${label}" || true
  elif kill -0 "${pid}" 2>/dev/null; then
    echo "Could not safely stop unregistered ${label} PID ${pid}; identity/session was not established." >&2
  fi
  return 1
}

absolute_from_experiment() {
  local path="$1"
  if [[ "${path}" = /* ]]; then
    printf '%s\n' "${path}"
  else
    printf '%s\n' "${EXPERIMENT_ROOT}/${path}"
  fi
}
