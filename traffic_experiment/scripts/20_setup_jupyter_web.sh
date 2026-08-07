#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
WEB_ROOT="${EXPERIMENT_ROOT}/web_control"
ACTION="${1:-install}"

JUPYTER_VENV_WAS_SET="${JUPYTER_VENV+x}"
JUPYTER_ROOT_DIR_WAS_SET="${JUPYTER_ROOT_DIR+x}"
JUPYTER_PORT_WAS_SET="${JUPYTER_PORT+x}"
JUPYTER_VENV="${JUPYTER_VENV:-${EXPERIMENT_ROOT}/.venv-jupyter}"
JUPYTER_ROOT_DIR="${JUPYTER_ROOT_DIR:-${REPOSITORY_ROOT}}"
JUPYTER_PORT="${JUPYTER_PORT:-8888}"

CONFIG_HOME="${HOME}/.config/commu-jupyter"
USER_UNIT_HOME="${HOME}/.config/systemd/user"
CONFIG_FILE="${CONFIG_HOME}/jupyter_server_config.py"
ENVIRONMENT_FILE="${CONFIG_HOME}/environment"
PASSWORD_HASH_FILE="${CONFIG_HOME}/password_hash"
LEGACY_TOKEN_FILE="${CONFIG_HOME}/token"
STATE_FILE="${CONFIG_HOME}/install.state"
LOCK_FILE="${CONFIG_HOME}/install.lock"
UNIT_FILE="${USER_UNIT_HOME}/commu-jupyter.service"
SERVICE_NAME="commu-jupyter.service"
VENV_OWNER_MARKER_NAME=".commu-jupyter-owner"

INSTALL_IN_PROGRESS=0
SERVICE_TOUCHED=0
VENV_CREATED=0
ARTIFACTS_CREATED=0
STAGING_DIR=""
OWNERSHIP_NONCE=""
CONFIG_SHA256=""
ENVIRONMENT_SHA256=""
PASSWORD_HASH_SHA256=""
UNIT_SHA256=""
JUPYTER_SHA256=""

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command not found: ${command_name}" >&2
    exit 2
  fi
}

current_uid() {
  id -u
}

file_mode() {
  stat -c %a "$1"
}

file_uid() {
  stat -c %u "$1"
}

file_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

canonical_existing_path() {
  readlink -f -- "$1"
}

canonical_target_path() {
  readlink -m -- "$1"
}

require_private_directory() {
  local path="$1" label="$2"
  if [[ ! -d "${path}" || -L "${path}" ]]; then
    echo "${label} is not a non-symlink directory: ${path}" >&2
    return 1
  fi
  if [[ "$(file_uid "${path}")" != "$(current_uid)" ]]; then
    echo "${label} is not owned by UID $(current_uid): ${path}" >&2
    return 1
  fi
  local mode
  mode="$(file_mode "${path}")"
  if ((8#${mode} & 8#022)); then
    echo "${label} must not be group/world writable: ${path} (mode ${mode})" >&2
    return 1
  fi
}

ensure_private_directory() {
  local path="$1" label="$2"
  if [[ -e "${path}" || -L "${path}" ]]; then
    require_private_directory "${path}" "${label}"
    return
  fi
  mkdir -m 0700 -p -- "${path}"
  require_private_directory "${path}" "${label}"
}

acquire_lifecycle_lock() {
  if [[ -e "${LOCK_FILE}" || -L "${LOCK_FILE}" ]]; then
    if [[ ! -f "${LOCK_FILE}" || -L "${LOCK_FILE}" ||
      "$(file_uid "${LOCK_FILE}")" != "$(current_uid)" ||
      "$(file_mode "${LOCK_FILE}")" != "600" ]]; then
      echo "Jupyter lifecycle lock is not a private owned regular file: ${LOCK_FILE}" >&2
      return 1
    fi
  fi
  exec 9>>"${LOCK_FILE}"
  chmod 0600 "${LOCK_FILE}"
  if [[ ! -f "${LOCK_FILE}" || -L "${LOCK_FILE}" ||
    "$(file_uid "${LOCK_FILE}")" != "$(current_uid)" ||
    "$(file_mode "${LOCK_FILE}")" != "600" ]]; then
    echo "Could not establish a private Jupyter lifecycle lock: ${LOCK_FILE}" >&2
    return 1
  fi
  flock -n 9 || {
    echo "Another Jupyter lifecycle action holds ${LOCK_FILE}." >&2
    return 1
  }
}

require_regular_owned_file() {
  local path="$1" expected_mode="$2" expected_sha="$3" label="$4"
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    echo "${label} is missing, non-regular, or a symlink: ${path}" >&2
    return 1
  fi
  if [[ "$(file_uid "${path}")" != "$(current_uid)" ]]; then
    echo "${label} is not owned by UID $(current_uid): ${path}" >&2
    return 1
  fi
  if [[ "$(file_mode "${path}")" != "${expected_mode}" ]]; then
    echo "${label} has unsafe mode $(file_mode "${path}"); expected ${expected_mode}: ${path}" >&2
    return 1
  fi
  if [[ ! "${expected_sha}" =~ ^[0-9a-f]{64}$ ||
    "$(file_sha256 "${path}")" != "${expected_sha}" ]]; then
    echo "${label} no longer matches the project-recorded SHA-256: ${path}" >&2
    return 1
  fi
}

require_state_file() {
  if [[ ! -f "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
    echo "No regular project Jupyter state is recorded at ${STATE_FILE}." >&2
    return 1
  fi
  if [[ "$(file_uid "${STATE_FILE}")" != "$(current_uid)" ||
    "$(file_mode "${STATE_FILE}")" != "600" ]]; then
    echo "Jupyter state must be owned by UID $(current_uid) with mode 0600: ${STATE_FILE}" >&2
    return 1
  fi
}

state_value() {
  local key="$1"
  local count value
  count="$(awk -F= -v key="${key}" '$1 == key {count++} END {print count+0}' "${STATE_FILE}")"
  if [[ "${count}" -ne 1 ]]; then
    echo "Jupyter state must contain exactly one ${key} field." >&2
    return 1
  fi
  value="$(awk -F= -v key="${key}" \
    '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${STATE_FILE}")"
  printf '%s\n' "${value}"
}

validate_owned_venv_path() {
  local venv_path="$1"
  local root_abs venv_abs
  root_abs="$(canonical_existing_path "${EXPERIMENT_ROOT}")"
  venv_abs="$(canonical_target_path "${venv_path}")"
  case "${venv_abs}" in
    "${root_abs}"/*) ;;
    *)
      echo "JUPYTER_VENV must stay below ${root_abs}; got ${venv_abs}." >&2
      return 1
      ;;
  esac
  if [[ "${venv_abs}" == "${root_abs}" ]]; then
    echo "JUPYTER_VENV must not be the experiment root itself." >&2
    return 1
  fi
  printf '%s\n' "${venv_abs}"
}

load_owned_state() {
  local mode="${1:-strict}"
  require_state_file

  local schema owner status recorded_service
  schema="$(state_value schema)"
  owner="$(state_value owner)"
  status="$(state_value status)"
  recorded_service="$(state_value service)"
  if [[ "${schema}" != "commu-jupyter-install-v2" ||
    "${owner}" != "commu-jupyter" ||
    "${recorded_service}" != "${SERVICE_NAME}" ]]; then
    echo "Jupyter state does not identify this project/service." >&2
    return 1
  fi
  case "${status}" in
    installed) ;;
    installing)
      [[ "${mode}" == "rollback" ]] || {
        echo "Jupyter installation state is incomplete (${status})." >&2
        return 1
      }
      ;;
    *)
      echo "Jupyter state has invalid status: ${status}" >&2
      return 1
      ;;
  esac

  local recorded_config recorded_environment recorded_hash recorded_state
  local recorded_unit recorded_venv recorded_root recorded_port
  recorded_config="$(state_value config_file)"
  recorded_environment="$(state_value environment_file)"
  recorded_hash="$(state_value password_hash_file)"
  recorded_state="$(state_value state_file)"
  recorded_unit="$(state_value unit_file)"
  recorded_venv="$(state_value jupyter_venv)"
  recorded_root="$(state_value jupyter_root_dir)"
  recorded_port="$(state_value jupyter_port)"
  if [[ "${recorded_config}" != "${CONFIG_FILE}" ||
    "${recorded_environment}" != "${ENVIRONMENT_FILE}" ||
    "${recorded_hash}" != "${PASSWORD_HASH_FILE}" ||
    "${recorded_state}" != "${STATE_FILE}" ||
    "${recorded_unit}" != "${UNIT_FILE}" ]]; then
    echo "Jupyter state paths do not match this installer." >&2
    return 1
  fi

  if [[ -n "${JUPYTER_VENV_WAS_SET}" &&
    "$(canonical_target_path "${JUPYTER_VENV}")" != "${recorded_venv}" ]]; then
    echo "JUPYTER_VENV does not match the recorded project installation." >&2
    return 1
  fi
  if [[ -n "${JUPYTER_ROOT_DIR_WAS_SET}" &&
    "$(canonical_existing_path "${JUPYTER_ROOT_DIR}")" != "${recorded_root}" ]]; then
    echo "JUPYTER_ROOT_DIR does not match the recorded project installation." >&2
    return 1
  fi
  if [[ -n "${JUPYTER_PORT_WAS_SET}" && "${JUPYTER_PORT}" != "${recorded_port}" ]]; then
    echo "JUPYTER_PORT does not match the recorded project installation." >&2
    return 1
  fi
  JUPYTER_VENV="${recorded_venv}"
  JUPYTER_ROOT_DIR="${recorded_root}"
  JUPYTER_PORT="${recorded_port}"
  validate_owned_venv_path "${JUPYTER_VENV}" >/dev/null
  if [[ ! -d "${JUPYTER_ROOT_DIR}" || -L "${JUPYTER_ROOT_DIR}" ]]; then
    echo "Recorded Jupyter root is missing or a symlink: ${JUPYTER_ROOT_DIR}" >&2
    return 1
  fi
  if [[ ! "${JUPYTER_PORT}" =~ ^[0-9]+$ ]] ||
    ((JUPYTER_PORT < 1024 || JUPYTER_PORT > 65535)); then
    echo "Recorded Jupyter port is invalid: ${JUPYTER_PORT}" >&2
    return 1
  fi

  OWNERSHIP_NONCE="$(state_value ownership_nonce)"
  if [[ ! "${OWNERSHIP_NONCE}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Jupyter ownership nonce is malformed." >&2
    return 1
  fi

  CONFIG_SHA256="$(state_value config_sha256)"
  ENVIRONMENT_SHA256="$(state_value environment_sha256)"
  PASSWORD_HASH_SHA256="$(state_value password_hash_sha256)"
  UNIT_SHA256="$(state_value unit_sha256)"
  JUPYTER_SHA256="$(state_value jupyter_sha256)"
  require_regular_owned_file "${CONFIG_FILE}" 600 "${CONFIG_SHA256}" "Jupyter config"
  require_regular_owned_file \
    "${ENVIRONMENT_FILE}" 600 "${ENVIRONMENT_SHA256}" "Jupyter environment"
  if [[ "${mode}" == "cleanup" || "${mode}" == "rollback" ]]; then
    if [[ ! -f "${PASSWORD_HASH_FILE}" || -L "${PASSWORD_HASH_FILE}" ||
      "$(file_sha256 "${PASSWORD_HASH_FILE}")" != "${PASSWORD_HASH_SHA256}" ]]; then
      echo "Jupyter password verifier no longer matches project state." >&2
      return 1
    fi
  else
    require_regular_owned_file \
      "${PASSWORD_HASH_FILE}" 600 "${PASSWORD_HASH_SHA256}" \
      "Jupyter password verifier"
  fi
  if [[ "$(awk 'END {print NR+0}' "${PASSWORD_HASH_FILE}")" -ne 1 ]] ||
    ! grep -Eq '^argon2:\$argon2(id|i|d)\$' "${PASSWORD_HASH_FILE}"; then
    echo "Jupyter password verifier is not one valid Argon2 verifier." >&2
    return 1
  fi
  if [[ "${mode}" == "strict" &&
    ( -e "${LEGACY_TOKEN_FILE}" || -L "${LEGACY_TOKEN_FILE}" ) ]]; then
    echo "Refusing to start with a legacy plaintext Jupyter token artifact: ${LEGACY_TOKEN_FILE}" >&2
    return 1
  fi
  require_regular_owned_file "${UNIT_FILE}" 600 "${UNIT_SHA256}" "Jupyter unit"

  local venv_uid venv_mode marker marker_sha jupyter_path jupyter_mode
  venv_uid="$(state_value venv_uid)"
  venv_mode="$(state_value venv_mode)"
  marker="${JUPYTER_VENV}/${VENV_OWNER_MARKER_NAME}"
  marker_sha="$(state_value venv_marker_sha256)"
  jupyter_path="$(state_value jupyter_executable)"
  jupyter_mode="$(state_value jupyter_mode)"
  if [[ ! -d "${JUPYTER_VENV}" || -L "${JUPYTER_VENV}" ||
    "$(file_uid "${JUPYTER_VENV}")" != "${venv_uid}" ||
    "$(file_mode "${JUPYTER_VENV}")" != "${venv_mode}" ]]; then
    echo "Recorded Jupyter virtual environment identity no longer matches." >&2
    return 1
  fi
  require_regular_owned_file "${marker}" 600 "${marker_sha}" "Jupyter venv marker"
  if ! grep -Fxq "ownership_nonce=${OWNERSHIP_NONCE}" "${marker}"; then
    echo "Jupyter virtual environment ownership nonce does not match state." >&2
    return 1
  fi
  if [[ "${jupyter_path}" != "${JUPYTER_VENV}/bin/jupyter" ]]; then
    echo "Recorded Jupyter executable is outside the owned virtual environment." >&2
    return 1
  fi
  require_regular_owned_file \
    "${jupyter_path}" "${jupyter_mode}" "${JUPYTER_SHA256}" \
    "Jupyter executable"
}

service_is_active() {
  systemctl --user is-active --quiet "${SERVICE_NAME}"
}

service_is_enabled() {
  systemctl --user is-enabled --quiet "${SERVICE_NAME}"
}

owned_loopback_listener_present() {
  local main_pid listeners
  main_pid="$(systemctl --user show --property=MainPID --value "${SERVICE_NAME}")"
  [[ "${main_pid}" =~ ^[1-9][0-9]*$ ]] || return 1
  listeners="$(ss -H -ltnp 2>/dev/null)" || return 1
  grep -E "127\\.0\\.0\\.1:${JUPYTER_PORT}[[:space:]]" <<<"${listeners}" |
    grep -Fq "pid=${main_pid},"
}

wait_for_authenticated_listener() {
  local attempt http_status
  for ((attempt = 0; attempt < 40; attempt++)); do
    if ! service_is_active || ! owned_loopback_listener_present; then
      sleep 0.25
      continue
    fi
    if http_status="$(
      curl --silent --show-error --max-time 2 \
        --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:${JUPYTER_PORT}/api/status" 2>/dev/null
    )" && [[ "${http_status}" == "403" ]]; then
      return 0
    fi
    sleep 0.25
  done
  echo "Jupyter did not expose an authentication-protected loopback API on port ${JUPYTER_PORT}." >&2
  return 1
}

require_service_fragment() {
  local fragment
  fragment="$(systemctl --user show \
    --property=FragmentPath --value "${SERVICE_NAME}")"
  if [[ -z "${fragment}" ||
    "$(canonical_existing_path "${fragment}")" != "$(canonical_existing_path "${UNIT_FILE}")" ]]; then
    echo "${SERVICE_NAME} is not loaded from the project-owned unit ${UNIT_FILE}." >&2
    return 1
  fi
}

disable_owned_service() {
  require_service_fragment
  local command_failed=0
  if ! systemctl --user disable --now "${SERVICE_NAME}"; then
    command_failed=1
  fi
  if service_is_active || service_is_enabled; then
    echo "${SERVICE_NAME} remains active or enabled after disable --now." >&2
    return 1
  fi
  if [[ "${command_failed}" -ne 0 ]]; then
    echo "systemctl reported an error, but inactive/disabled state was verified." >&2
  fi
}

write_state() {
  local status="$1"
  local marker="${JUPYTER_VENV}/${VENV_OWNER_MARKER_NAME}"
  local state_tmp
  state_tmp="$(mktemp "${CONFIG_HOME}/.install-state.XXXXXX")"
  {
    printf 'schema=commu-jupyter-install-v2\n'
    printf 'owner=commu-jupyter\n'
    printf 'status=%s\n' "${status}"
    printf 'service=%s\n' "${SERVICE_NAME}"
    printf 'ownership_nonce=%s\n' "${OWNERSHIP_NONCE}"
    printf 'config_file=%s\n' "${CONFIG_FILE}"
    printf 'config_sha256=%s\n' "${CONFIG_SHA256}"
    printf 'environment_file=%s\n' "${ENVIRONMENT_FILE}"
    printf 'environment_sha256=%s\n' "${ENVIRONMENT_SHA256}"
    printf 'password_hash_file=%s\n' "${PASSWORD_HASH_FILE}"
    printf 'password_hash_sha256=%s\n' "${PASSWORD_HASH_SHA256}"
    printf 'state_file=%s\n' "${STATE_FILE}"
    printf 'unit_file=%s\n' "${UNIT_FILE}"
    printf 'unit_sha256=%s\n' "${UNIT_SHA256}"
    printf 'jupyter_venv=%s\n' "${JUPYTER_VENV}"
    printf 'venv_uid=%s\n' "$(file_uid "${JUPYTER_VENV}")"
    printf 'venv_mode=%s\n' "$(file_mode "${JUPYTER_VENV}")"
    printf 'venv_marker_sha256=%s\n' "$(file_sha256 "${marker}")"
    printf 'jupyter_executable=%s\n' "${JUPYTER_VENV}/bin/jupyter"
    printf 'jupyter_mode=%s\n' "$(file_mode "${JUPYTER_VENV}/bin/jupyter")"
    printf 'jupyter_sha256=%s\n' "${JUPYTER_SHA256}"
    printf 'jupyter_root_dir=%s\n' "${JUPYTER_ROOT_DIR}"
    printf 'jupyter_port=%s\n' "${JUPYTER_PORT}"
    printf 'updated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${state_tmp}"
  chmod 0600 "${state_tmp}"
  mv -f -- "${state_tmp}" "${STATE_FILE}"
}

safe_remove_created_venv() {
  [[ "${VENV_CREATED}" -eq 1 && -n "${OWNERSHIP_NONCE}" ]] || return 0
  local expected marker
  expected="$(validate_owned_venv_path "${JUPYTER_VENV}")" || return 1
  marker="${expected}/${VENV_OWNER_MARKER_NAME}"
  if [[ ! -f "${marker}" || -L "${marker}" ||
    "$(file_uid "${marker}")" != "$(current_uid)" ]] ||
    ! grep -Fxq "ownership_nonce=${OWNERSHIP_NONCE}" "${marker}"; then
    echo "Refusing to remove virtual environment without its exact ownership marker: ${expected}" >&2
    return 1
  fi
  rm -rf -- "${expected}"
  VENV_CREATED=0
}

remove_created_artifact() {
  local path="$1" expected_sha="$2" label="$3"
  if [[ ! -e "${path}" && ! -L "${path}" ]]; then
    return 0
  fi
  if ! require_regular_owned_file "${path}" 600 "${expected_sha}" "${label}"; then
    echo "Refusing to remove a changed ${label} during rollback." >&2
    return 1
  fi
  rm -f -- "${path}"
}

remove_created_state() {
  if [[ ! -e "${STATE_FILE}" && ! -L "${STATE_FILE}" ]]; then
    return 0
  fi
  if ! require_state_file ||
    ! grep -Fxq "schema=commu-jupyter-install-v2" "${STATE_FILE}" ||
    ! grep -Fxq "ownership_nonce=${OWNERSHIP_NONCE}" "${STATE_FILE}"; then
    echo "Refusing to remove changed Jupyter state during rollback." >&2
    return 1
  fi
  rm -f -- "${STATE_FILE}"
}

rollback_install() {
  local status=$?
  local rollback_failed=0
  trap - EXIT INT TERM HUP
  if [[ "${INSTALL_IN_PROGRESS}" -eq 1 ]]; then
    if [[ "${SERVICE_TOUCHED}" -eq 1 ]]; then
      if load_owned_state rollback && require_service_fragment; then
        disable_owned_service || rollback_failed=1
      else
        echo "Refusing to stop a service whose project identity cannot be verified." >&2
        rollback_failed=1
      fi
    fi
    if [[ "${ARTIFACTS_CREATED}" -eq 1 ]]; then
      remove_created_artifact \
        "${UNIT_FILE}" "${UNIT_SHA256}" "Jupyter unit" || rollback_failed=1
      remove_created_artifact \
        "${CONFIG_FILE}" "${CONFIG_SHA256}" "Jupyter config" || rollback_failed=1
      remove_created_artifact \
        "${ENVIRONMENT_FILE}" "${ENVIRONMENT_SHA256}" \
        "Jupyter environment" || rollback_failed=1
      remove_created_artifact \
        "${PASSWORD_HASH_FILE}" "${PASSWORD_HASH_SHA256}" \
        "Jupyter password verifier" || rollback_failed=1
      remove_created_state || rollback_failed=1
      systemctl --user daemon-reload || rollback_failed=1
    fi
    if [[ -n "${STAGING_DIR}" ]]; then
      case "${STAGING_DIR}" in
        "${CONFIG_HOME}"/.install-staging.*)
          rm -rf -- "${STAGING_DIR}" || rollback_failed=1
          ;;
        *)
          echo "Refusing to remove unexpected staging directory: ${STAGING_DIR}" >&2
          rollback_failed=1
          ;;
      esac
    fi
    safe_remove_created_venv || rollback_failed=1
  fi
  if [[ "${rollback_failed}" -ne 0 ]]; then
    echo "Jupyter installation rollback was incomplete; inspect ${CONFIG_HOME}." >&2
    [[ "${status}" -ne 0 ]] || status=1
  fi
  exit "${status}"
}

prompt_password_hash() {
  local destination="$1"
  local password="" confirmation="" xtrace_was_enabled=0
  if [[ ! -t 0 ]]; then
    echo "Interactive input is required to set the Jupyter password." >&2
    return 2
  fi
  case "$-" in
    *x*) xtrace_was_enabled=1; set +x ;;
  esac
  printf 'Jupyter password (minimum 12 characters): ' >&2
  IFS= read -r -s password
  printf '\nConfirm Jupyter password: ' >&2
  IFS= read -r -s confirmation
  printf '\n' >&2
  if [[ "${#password}" -lt 12 ]]; then
    echo "Jupyter password must contain at least 12 characters." >&2
    unset password confirmation
    [[ "${xtrace_was_enabled}" -eq 0 ]] || set -x
    return 2
  fi
  if [[ "${password}" != "${confirmation}" ]]; then
    echo "Jupyter password confirmation did not match." >&2
    unset password confirmation
    [[ "${xtrace_was_enabled}" -eq 0 ]] || set -x
    return 2
  fi
  if ! printf '%s\n' "${password}" |
    "${JUPYTER_VENV}/bin/python" -c \
      'import sys; from jupyter_server.auth import passwd; print(passwd(sys.stdin.readline().rstrip("\n"), algorithm="argon2"))' \
      >"${destination}"; then
    unset password confirmation
    [[ "${xtrace_was_enabled}" -eq 0 ]] || set -x
    return 1
  fi
  unset password confirmation
  [[ "${xtrace_was_enabled}" -eq 0 ]] || set -x
  chmod 0600 "${destination}"
  if [[ "$(file_uid "${destination}")" != "$(current_uid)" ]] ||
    ! grep -Eq '^argon2:\$argon2(id|i|d)\$' "${destination}"; then
    echo "Jupyter password verifier generation failed closed." >&2
    return 1
  fi
}

install_service() {
  local command_name
  for command_name in \
    python3 systemctl flock sha256sum stat readlink mktemp install grep curl sleep ss; do
    require_command "${command_name}"
  done
  if [[ "${EUID}" -eq 0 ]]; then
    echo "Do not run JupyterLab as root. Run this as the dedicated lab user." >&2
    exit 2
  fi
  if [[ ! -d "${JUPYTER_ROOT_DIR}" || -L "${JUPYTER_ROOT_DIR}" ]]; then
    echo "JUPYTER_ROOT_DIR must be an existing non-symlink directory: ${JUPYTER_ROOT_DIR}" >&2
    exit 2
  fi
  JUPYTER_ROOT_DIR="$(canonical_existing_path "${JUPYTER_ROOT_DIR}")"
  JUPYTER_VENV="$(validate_owned_venv_path "${JUPYTER_VENV}")"
  if [[ ! "${JUPYTER_PORT}" =~ ^[0-9]+$ ]] ||
    ((JUPYTER_PORT < 1024 || JUPYTER_PORT > 65535)); then
    echo "JUPYTER_PORT must be an unprivileged TCP port; got ${JUPYTER_PORT}." >&2
    exit 2
  fi

  umask 077
  ensure_private_directory "${CONFIG_HOME}" "Jupyter config directory"
  ensure_private_directory "${USER_UNIT_HOME}" "systemd user unit directory"
  acquire_lifecycle_lock || exit 2

  if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
    load_owned_state strict
    echo "Jupyter is already installed and identity-verified; use start, stop, status, or restore." >&2
    exit 2
  fi
  if [[ -e "${LEGACY_TOKEN_FILE}" || -L "${LEGACY_TOKEN_FILE}" ]]; then
    echo "Refusing to install while a legacy plaintext token artifact exists: ${LEGACY_TOKEN_FILE}" >&2
    exit 2
  fi
  local target
  for target in \
    "${CONFIG_FILE}" "${ENVIRONMENT_FILE}" "${PASSWORD_HASH_FILE}" "${UNIT_FILE}"; do
    if [[ -e "${target}" || -L "${target}" ]]; then
      echo "Refusing to overwrite unowned Jupyter artifact: ${target}" >&2
      exit 2
    fi
  done
  if [[ -e "${JUPYTER_VENV}" || -L "${JUPYTER_VENV}" ]]; then
    echo "Refusing to adopt an existing JUPYTER_VENV: ${JUPYTER_VENV}" >&2
    exit 2
  fi
  if service_is_active || service_is_enabled; then
    echo "Refusing to replace an existing ${SERVICE_NAME} without project state." >&2
    exit 2
  fi

  INSTALL_IN_PROGRESS=1
  trap rollback_install EXIT INT TERM HUP
  OWNERSHIP_NONCE="$(python3 -c 'import os; print(os.urandom(32).hex())')"
  mkdir -m 0700 -- "${JUPYTER_VENV}"
  VENV_CREATED=1
  {
    printf 'schema=commu-jupyter-venv-v1\n'
    printf 'ownership_nonce=%s\n' "${OWNERSHIP_NONCE}"
    printf 'jupyter_venv=%s\n' "${JUPYTER_VENV}"
  } >"${JUPYTER_VENV}/${VENV_OWNER_MARKER_NAME}"
  chmod 0600 "${JUPYTER_VENV}/${VENV_OWNER_MARKER_NAME}"
  python3 -m venv "${JUPYTER_VENV}"

  "${JUPYTER_VENV}/bin/python" -m pip install --upgrade pip
  "${JUPYTER_VENV}/bin/python" -m pip install \
    -r "${WEB_ROOT}/requirements-jupyter.txt"
  if [[ ! -f "${JUPYTER_VENV}/bin/jupyter" ||
    -L "${JUPYTER_VENV}/bin/jupyter" ]]; then
    echo "Installed Jupyter executable is missing or a symlink." >&2
    exit 1
  fi

  STAGING_DIR="$(mktemp -d "${CONFIG_HOME}/.install-staging.XXXXXX")"
  cp -- "${WEB_ROOT}/jupyter_server_config.py" "${STAGING_DIR}/jupyter_server_config.py"
  cp -- "${WEB_ROOT}/commu-jupyter.service" "${STAGING_DIR}/commu-jupyter.service"
  {
    printf 'JUPYTER_VENV=%q\n' "${JUPYTER_VENV}"
    printf 'JUPYTER_ROOT_DIR=%q\n' "${JUPYTER_ROOT_DIR}"
    printf 'JUPYTER_PORT=%q\n' "${JUPYTER_PORT}"
    printf 'JUPYTER_CONFIG_FILE=%q\n' "${CONFIG_FILE}"
  } >"${STAGING_DIR}/environment"
  prompt_password_hash "${STAGING_DIR}/password_hash"
  chmod 0600 \
    "${STAGING_DIR}/jupyter_server_config.py" \
    "${STAGING_DIR}/commu-jupyter.service" \
    "${STAGING_DIR}/environment"

  # Recheck the shared user-service namespace immediately before deployment.
  for target in \
    "${CONFIG_FILE}" "${ENVIRONMENT_FILE}" "${PASSWORD_HASH_FILE}" "${UNIT_FILE}"; do
    if [[ -e "${target}" || -L "${target}" ]]; then
      echo "Refusing to overwrite concurrently created Jupyter artifact: ${target}" >&2
      exit 2
    fi
  done
  if service_is_active || service_is_enabled; then
    echo "Refusing to replace a concurrently created ${SERVICE_NAME}." >&2
    exit 2
  fi

  CONFIG_SHA256="$(file_sha256 "${STAGING_DIR}/jupyter_server_config.py")"
  ENVIRONMENT_SHA256="$(file_sha256 "${STAGING_DIR}/environment")"
  PASSWORD_HASH_SHA256="$(file_sha256 "${STAGING_DIR}/password_hash")"
  UNIT_SHA256="$(file_sha256 "${STAGING_DIR}/commu-jupyter.service")"
  JUPYTER_SHA256="$(file_sha256 "${JUPYTER_VENV}/bin/jupyter")"
  ARTIFACTS_CREATED=1
  install -m 0600 "${STAGING_DIR}/jupyter_server_config.py" "${CONFIG_FILE}"
  install -m 0600 "${STAGING_DIR}/environment" "${ENVIRONMENT_FILE}"
  install -m 0600 "${STAGING_DIR}/password_hash" "${PASSWORD_HASH_FILE}"
  install -m 0600 "${STAGING_DIR}/commu-jupyter.service" "${UNIT_FILE}"
  rm -rf -- "${STAGING_DIR}"
  STAGING_DIR=""

  require_regular_owned_file "${CONFIG_FILE}" 600 "${CONFIG_SHA256}" "Jupyter config"
  require_regular_owned_file \
    "${ENVIRONMENT_FILE}" 600 "${ENVIRONMENT_SHA256}" "Jupyter environment"
  require_regular_owned_file \
    "${PASSWORD_HASH_FILE}" 600 "${PASSWORD_HASH_SHA256}" \
    "Jupyter password verifier"
  require_regular_owned_file "${UNIT_FILE}" 600 "${UNIT_SHA256}" "Jupyter unit"
  write_state installing
  load_owned_state rollback

  systemctl --user daemon-reload
  require_service_fragment
  SERVICE_TOUCHED=1
  systemctl --user enable --now "${SERVICE_NAME}"
  if ! service_is_enabled || ! wait_for_authenticated_listener; then
    echo "${SERVICE_NAME} did not become active, enabled, and authentication-protected." >&2
    exit 1
  fi
  write_state installed
  load_owned_state strict

  INSTALL_IN_PROGRESS=0
  trap - EXIT INT TERM HUP
  echo
  echo "JupyterLab is listening only at http://127.0.0.1:${JUPYTER_PORT}."
  echo "Authentication uses an interactive password; only its Argon2 verifier is stored."
  echo "Next: configure one approved HTTPS ingress method from docs/jupyter_web_access.md."
}

prepare_owned_action() {
  local mode="${1:-strict}"
  local command_name
  for command_name in systemctl flock sha256sum stat readlink grep curl sleep ss; do
    require_command "${command_name}"
  done
  if [[ "${EUID}" -eq 0 ]]; then
    echo "Run Jupyter user-service actions as the owning non-root lab user." >&2
    exit 2
  fi
  require_private_directory "${CONFIG_HOME}" "Jupyter config directory"
  require_private_directory "${USER_UNIT_HOME}" "systemd user unit directory"
  acquire_lifecycle_lock || exit 2
  load_owned_state "${mode}"
  systemctl --user daemon-reload
  require_service_fragment
}

start_service() {
  prepare_owned_action strict
  systemctl --user enable --now "${SERVICE_NAME}"
  if ! service_is_enabled || ! wait_for_authenticated_listener; then
    echo "${SERVICE_NAME} did not become active, enabled, and authentication-protected." >&2
    if ! disable_owned_service; then
      echo "Failed to roll back the project-owned service after start failure." >&2
    fi
    exit 1
  fi
  echo "${SERVICE_NAME} is active, enabled, and identity-verified."
}

stop_service() {
  prepare_owned_action cleanup
  disable_owned_service
  echo "${SERVICE_NAME} is inactive and disabled."
}

status_service() {
  prepare_owned_action strict
  systemctl --user --no-pager --full status "${SERVICE_NAME}"
}

restore_installation() {
  prepare_owned_action cleanup
  local recorded_venv="${JUPYTER_VENV}"
  disable_owned_service
  rm -f -- \
    "${UNIT_FILE}" "${CONFIG_FILE}" "${ENVIRONMENT_FILE}" \
    "${PASSWORD_HASH_FILE}" "${STATE_FILE}"
  systemctl --user daemon-reload
  if service_is_active || service_is_enabled; then
    echo "Failed to restore inactive/disabled ${SERVICE_NAME} state." >&2
    exit 1
  fi
  rm -f -- "${LOCK_FILE}"
  rmdir "${CONFIG_HOME}" 2>/dev/null || true
  echo "Removed the identity-verified Jupyter service and authentication artifacts."
  echo "The owned virtual environment was preserved at ${recorded_venv}."
  echo "Remove that directory explicitly before a fresh install."
}

case "${ACTION}" in
  install) install_service ;;
  start) start_service ;;
  stop) stop_service ;;
  status) status_service ;;
  restore | uninstall) restore_installation ;;
  *)
    echo "Usage: $0 {install|start|stop|status|restore}" >&2
    exit 2
    ;;
esac
