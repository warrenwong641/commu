#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ACTION="${1:-start}"
VLLM_SECONDARY_PORT="${VLLM_SECONDARY_PORT:-$((VLLM_PORT + 1))}"
SECURE_PROXY_HOST="${SECURE_PROXY_HOST:-localhost}"
export VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT SECURE_PROXY_HOST
CADDYFILE="${EXPERIMENT_ROOT}/configs/Caddyfile"
CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
mkdir -p "${CADDY_RUN_DIR}"
export XDG_DATA_HOME="${CADDY_RUN_DIR}/data"
export XDG_CONFIG_HOME="${CADDY_RUN_DIR}/config"
CADDY_STATE_FILE="$(absolute_from_experiment "${CADDY_STATE_FILE:-${CADDY_RUN_DIR}/secure_proxy.state}")"
CADDY_LOG_FILE="${CADDY_LOG_FILE:-${CADDY_STATE_FILE}.log}"
CADDY_EXE="$(command -v caddy || true)"
START_IN_PROGRESS=0
EXPECTED_PROXY_TCP_PORTS=(8443 8543)
EXPECTED_PROXY_UDP_PORTS=(8444 8544)

refuse_unsafe_artifact_target() {
  local label="$1" path="$2"
  if [[ -L "${path}" ]]; then
    echo "Refusing symlinked ${label} target: ${path}" >&2
    return 1
  fi
  if [[ -e "${path}" && ! -f "${path}" ]]; then
    echo "Refusing non-regular ${label} target: ${path}" >&2
    return 1
  fi
}

prepare_proxy_artifacts() {
  refuse_unsafe_artifact_target "proxy state" "${CADDY_STATE_FILE}"
  refuse_unsafe_artifact_target "proxy log" "${CADDY_LOG_FILE}"
  mkdir -p \
    "$(dirname -- "${CADDY_STATE_FILE}")" \
    "$(dirname -- "${CADDY_LOG_FILE}")"
}

state_value() {
  local key="$1"
  [[ -f "${CADDY_STATE_FILE}" ]] || return 0
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' \
    "${CADDY_STATE_FILE}"
}

process_start_ticks() {
  local pid="$1"
  awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true
}

caddy_pid_matches() {
  local pid="$1" expected_ticks="$2" expected_config="$3" expected_exe="$4"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" &&
    -n "${expected_config}" && -n "${expected_exe}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  local actual_exe expected_real_exe
  actual_exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
  expected_real_exe="$(readlink -f "${expected_exe}" 2>/dev/null || true)"
  [[ "${actual_exe}" == "${expected_real_exe}" ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/${pid}/cmdline" || return 1
  local index saw_run=0 saw_config=0
  for ((index = 0; index < ${#argv[@]}; index++)); do
    [[ "${argv[index]}" == "run" ]] && saw_run=1
    if [[ "${argv[index]}" == "--config" &&
      "${argv[index + 1]:-}" == "${expected_config}" ]]; then
      saw_config=1
    fi
  done
  [[ "${saw_run}" -eq 1 && "${saw_config}" -eq 1 ]]
}

proxy_listener_ports_closed() {
  local tcp_listeners udp_listeners port
  if ! command -v ss >/dev/null 2>&1; then
    echo "Cannot verify proxy listener closure because ss is unavailable." >&2
    return 1
  fi
  if ! tcp_listeners="$(ss -H -ltn 2>/dev/null)" ||
    ! udp_listeners="$(ss -H -lun 2>/dev/null)"; then
    echo "Failed to inspect TCP/UDP listeners after stopping Caddy." >&2
    return 1
  fi
  for port in "${EXPECTED_PROXY_TCP_PORTS[@]}"; do
    if grep -Eq ":${port}[[:space:]]" <<<"${tcp_listeners}"; then
      echo "A TCP listener remains on expected proxy port ${port}." >&2
      return 1
    fi
  done
  for port in "${EXPECTED_PROXY_UDP_PORTS[@]}"; do
    if grep -Eq ":${port}[[:space:]]" <<<"${udp_listeners}"; then
      echo "A UDP listener remains on expected proxy port ${port}." >&2
      return 1
    fi
  done
}

stop_recorded_caddy() {
  [[ -e "${CADDY_STATE_FILE}" || -L "${CADDY_STATE_FILE}" ]] || return 0
  refuse_unsafe_artifact_target "proxy state" "${CADDY_STATE_FILE}" || return 1
  local pid expected_ticks expected_config expected_exe
  pid="$(state_value caddy_pid)"
  expected_ticks="$(state_value caddy_start_ticks)"
  expected_config="$(state_value caddy_config)"
  expected_exe="$(state_value caddy_exe)"
  if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
    echo "Refusing to remove malformed proxy PID state at ${CADDY_STATE_FILE}." >&2
    return 1
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    if [[ -e "/proc/${pid}/stat" ]]; then
      echo "Cannot verify whether recorded Caddy PID ${pid} is still owned; preserving state." >&2
      return 1
    fi
    if ! proxy_listener_ports_closed; then
      echo "Recorded Caddy PID ${pid} is absent but proxy listener closure is unverified; preserving state." >&2
      return 1
    fi
    rm -f "${CADDY_STATE_FILE}"
    return 0
  fi
  if ! caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
    echo "Refusing to stop PID ${pid}: executable, config, or start metadata does not match." >&2
    return 1
  fi
  kill -TERM "${pid}" 2>/dev/null || true
  local attempt
  for attempt in {1..50}; do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.1
  done
  if caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
    kill -KILL "${pid}" 2>/dev/null || true
    for attempt in {1..20}; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.1
    done
    if caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
      echo "Verified Caddy PID ${pid} did not exit; preserving ownership state." >&2
      return 1
    fi
  elif kill -0 "${pid}" 2>/dev/null; then
    echo "PID ${pid} changed identity during shutdown; refusing further signals." >&2
    return 1
  fi
  if kill -0 "${pid}" 2>/dev/null; then
    echo "PID ${pid} changed identity or became unverifiable during shutdown; preserving state." >&2
    return 1
  fi
  if ! proxy_listener_ports_closed; then
    echo "Caddy exited but expected proxy listeners remain or could not be verified; preserving state." >&2
    return 1
  fi
  rm -f "${CADDY_STATE_FILE}"
}

cleanup_failed_start() {
  local status=$?
  trap - EXIT
  if [[ "${START_IN_PROGRESS}" -eq 1 ]]; then
    if ! stop_recorded_caddy; then
      echo "Failed-start cleanup incomplete; preserving ${CADDY_STATE_FILE}." >&2
      [[ "${status}" -ne 0 ]] || status=1
    fi
  fi
  exit "${status}"
}
trap cleanup_failed_start EXIT

start_proxy() {
  prepare_proxy_artifacts
  require_command caddy
  CADDY_EXE="$(command -v caddy)"
  if [[ -e "${CADDY_STATE_FILE}" ]]; then
    local recorded_pid
    recorded_pid="$(state_value caddy_pid)"
    if [[ "${recorded_pid}" =~ ^[0-9]+$ ]] && ! kill -0 "${recorded_pid}" 2>/dev/null; then
      if [[ -e "/proc/${recorded_pid}/stat" ]] ||
        ! proxy_listener_ports_closed; then
        echo "Refusing to replace stale proxy state until process absence and listener closure are verified." >&2
        return 1
      fi
      rm -f "${CADDY_STATE_FILE}"
    else
      echo "Refusing to replace existing proxy state at ${CADDY_STATE_FILE}." >&2
      echo "Inspect it and run '$0 stop' with the same CADDY_STATE_FILE." >&2
      return 1
    fi
  fi

  caddy validate --config "${CADDYFILE}" --adapter caddyfile
  {
    echo "status=starting"
    echo "caddy_config=${CADDYFILE}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "secure_proxy_host=${SECURE_PROXY_HOST}"
    echo "activation_requested_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${CADDY_STATE_FILE}"
  START_IN_PROGRESS=1
  caddy run --config "${CADDYFILE}" --adapter caddyfile >>"${CADDY_LOG_FILE}" 2>&1 &
  local caddy_pid=$!
  local caddy_start_ticks
  caddy_start_ticks="$(process_start_ticks "${caddy_pid}")"
  {
    echo "status=starting"
    echo "caddy_pid=${caddy_pid}"
    echo "caddy_start_ticks=${caddy_start_ticks}"
    echo "caddy_config=${CADDYFILE}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "secure_proxy_host=${SECURE_PROXY_HOST}"
    echo "activation_requested_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${CADDY_STATE_FILE}"
  sleep 1
  if ! caddy_pid_matches "${caddy_pid}" "${caddy_start_ticks}" "${CADDYFILE}" "${CADDY_EXE}"; then
    echo "Caddy failed to start; check ${CADDY_LOG_FILE}." >&2
    return 1
  fi
  {
    echo "status=running"
    echo "caddy_pid=${caddy_pid}"
    echo "caddy_start_ticks=${caddy_start_ticks}"
    echo "caddy_config=${CADDYFILE}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "secure_proxy_host=${SECURE_PROXY_HOST}"
    echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${CADDY_STATE_FILE}"
  START_IN_PROGRESS=0

  local ca_file
  ca_file="${XDG_DATA_HOME}/caddy/pki/authorities/local/root.crt"
  echo "GPU 0 proxy: TLS 1.3/H1 on TCP 8443; HTTP/3 on UDP 8444."
  echo "GPU 1 proxy: TLS 1.3/H1 on TCP 8543; HTTP/3 on UDP 8544."
  echo "Secure proxy host: ${SECURE_PROXY_HOST}"
  echo "CA certificate: ${ca_file}"
  echo "Proxy state: ${CADDY_STATE_FILE}"
  echo "Verify curl HTTP/3 support with: curl --version"
}

status_proxy() {
  if [[ ! -e "${CADDY_STATE_FILE}" && ! -L "${CADDY_STATE_FILE}" ]]; then
    echo "Caddy proxy is not recorded as running."
    return 1
  fi
  refuse_unsafe_artifact_target "proxy state" "${CADDY_STATE_FILE}" || return 1
  local pid expected_ticks expected_config expected_exe
  pid="$(state_value caddy_pid)"
  expected_ticks="$(state_value caddy_start_ticks)"
  expected_config="$(state_value caddy_config)"
  expected_exe="$(state_value caddy_exe)"
  if caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
    echo "Caddy proxy PID ${pid} is running with verified ownership."
  else
    echo "Caddy proxy state is stale or does not match PID ${pid:-unknown}." >&2
    return 1
  fi
}

case "${ACTION}" in
  start) start_proxy ;;
  stop) stop_recorded_caddy ;;
  status) status_proxy ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
