#!/usr/bin/env bash
# Server-side physical-interface listener for client-to-server validation.
# Binds Caddy on 144.214.210.31:8443 (TLS 1.3/TCP) + :8444 (HTTP/3/UDP)
# plus secondary worker ports :8543/:8544.  vLLM backend on 127.0.0.1:8000+8001.
#
# Does NOT mutate firewall rules.  Produces PID file, state file, and
# append-only log under runs/physical_validation/server/.
#
# Usage:
#   sudo --preserve-env=PATH bash scripts/23_server_physical_listener.sh start
#   sudo --preserve-env=PATH bash scripts/23_server_physical_listener.sh status
#   sudo --preserve-env=PATH bash scripts/23_server_physical_listener.sh stop
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

die() { printf '%s\n' "$*" >&2; exit 1; }

ACTION="${1:-start}"
LISTENER_PROFILE="${LISTENER_PROFILE:-high_ports}"
PHYS_IF="${PHYS_IF:-ens20f0}"
PHYS_IP=""

case "${LISTENER_PROFILE}" in
  high_ports)
    TLS_PORT=8443; H3_PORT=8444; W2_TLS=8543; W2_H3=8544 ;;
  standard_https)
    TLS_PORT=443;  H3_PORT=443;  W2_TLS=8543; W2_H3=8544 ;;
  *) die "LISTENER_PROFILE must be high_ports or standard_https" ;;
esac

STATE_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/physical_validation/server/${LISTENER_PROFILE}"
mkdir -p "${STATE_DIR}"
PID_FILE="${STATE_DIR}/caddy.pid"
STATE_FILE="${STATE_DIR}/listener_state"
LOG_FILE="${STATE_DIR}/caddy.log"
CADDY_EXE="$(command -v caddy || true)"
START_IN_PROGRESS=0

_caddyfile() {
  # When TLS_PORT == H3_PORT (standard_https on 443), Caddy requires a
  # single non-duplicated server entry with both protocols listed.
  # When ports differ (high_ports), separate entries are fine.
  if [[ "${TLS_PORT}" == "${H3_PORT}" ]]; then
    # Shared port: single servers entry with h1+h3, single site block.
    # Caddy serves HTTP/1.1 over TCP and HTTP/3 over UDP from one site.
    cat <<CEOF
{
  auto_https disable_redirects
  servers :${TLS_PORT} {
    protocols h1 h3
  }
  servers :${W2_TLS} {
    protocols h1
  }
  servers :${W2_H3} {
    protocols h3
  }
}

https://${PHYS_IP}:${TLS_PORT} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:${W2_TLS} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
https://${PHYS_IP}:${W2_H3} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
CEOF
  else
    cat <<CEOF
{
  auto_https disable_redirects
  servers :${TLS_PORT} {
    protocols h1
  }
  servers :${H3_PORT} {
    protocols h3
  }
  servers :${W2_TLS} {
    protocols h1
  }
  servers :${W2_H3} {
    protocols h3
  }
}

https://${PHYS_IP}:${TLS_PORT} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:${H3_PORT} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:${W2_TLS} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
https://${PHYS_IP}:${W2_H3} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
CEOF
  fi
}

_caddy_data() {
  CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
  mkdir -p "${CADDY_RUN_DIR}"
  export XDG_DATA_HOME="${CADDY_RUN_DIR}/data"
  export XDG_CONFIG_HOME="${CADDY_RUN_DIR}/config"
}

_state_value() {
  local key="$1"
  [[ -f "${STATE_FILE}" ]] || return 0
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${STATE_FILE}"
}

_process_start_ticks() {
  local pid="$1"
  awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true
}

_discover_physical_ip() {
  ip -4 -o addr show dev "${PHYS_IF}" scope global 2>/dev/null |
    awk '{print $4}' | cut -d/ -f1 | head -1
}

_caddy_pid_matches() {
  local pid="$1" expected_ticks="$2" expected_config="$3" expected_exe="$4"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" &&
    -n "${expected_config}" && -n "${expected_exe}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(_process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  local actual_exe expected_real_exe
  actual_exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
  expected_real_exe="$(readlink -f "${expected_exe}" 2>/dev/null || true)"
  [[ "${actual_exe}" == "${expected_real_exe}" ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/${pid}/cmdline" || return 1
  local index saw_run=0 saw_config=0
  for ((index = 0; index < ${#argv[@]}; index++)); do
    [[ "${argv[index]}" == "run" ]] && saw_run=1
    if [[ "${argv[index]}" == "--config" && "${argv[index + 1]:-}" == "${expected_config}" ]]; then
      saw_config=1
    fi
  done
  [[ "${saw_run}" -eq 1 && "${saw_config}" -eq 1 ]]
}

_wait_for_verified_caddy_exit() {
  local pid="$1" expected_ticks="$2" expected_config="$3" expected_exe="$4" attempts="$5"
  local attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    if ! _caddy_pid_matches \
      "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
      # The recorded process exited and the PID may have been reused.
      return 0
    fi
    sleep 0.1
  done
  return 1
}

_listener_ports_closed() {
  local tcp_listeners udp_listeners
  if ! command -v ss >/dev/null 2>&1; then
    echo "Cannot verify listener closure because ss is unavailable." >&2
    return 1
  fi
  if ! tcp_listeners="$(ss -H -ltn 2>/dev/null)" ||
    ! udp_listeners="$(ss -H -lun 2>/dev/null)"; then
    echo "Failed to inspect TCP/UDP listeners after stopping Caddy." >&2
    return 1
  fi
  if grep -Eq ":(${TLS_PORT}|${W2_TLS})[[:space:]]" <<<"${tcp_listeners}"; then
    echo "A TCP listener remains on ${TLS_PORT} or ${W2_TLS}." >&2
    return 1
  fi
  if grep -Eq ":(${H3_PORT}|${W2_H3})[[:space:]]" <<<"${udp_listeners}"; then
    echo "A UDP listener remains on ${H3_PORT} or ${W2_H3}." >&2
    return 1
  fi
}

_stop_verified_caddy() {
  local pid="$1" expected_ticks="$2" expected_config="$3" expected_exe="$4"
  if ! _caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
    if kill -0 "${pid}" 2>/dev/null; then
      return 1
    fi
    _listener_ports_closed
    return
  fi
  kill -TERM "${pid}" 2>/dev/null || true
  if ! _wait_for_verified_caddy_exit \
    "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}" 50; then
    kill -KILL "${pid}" 2>/dev/null || true
    if ! _wait_for_verified_caddy_exit \
      "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}" 20; then
      echo "Verified Caddy PID ${pid} did not exit." >&2
      return 1
    fi
  fi
  _listener_ports_closed
}

_cleanup_failed_start() {
  local status=$?
  trap - EXIT
  if [[ "${START_IN_PROGRESS}" -eq 1 ]]; then
    if [[ -f "${PID_FILE}" ]]; then
      local pid expected_ticks expected_config expected_exe
      pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
      expected_ticks="$(_state_value caddy_start_ticks)"
      expected_config="$(_state_value caddy_config)"
      expected_exe="$(_state_value caddy_exe)"
      if [[ "${pid}" =~ ^[0-9]+$ ]] &&
        _stop_verified_caddy "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
        rm -f "${PID_FILE}" "${STATE_FILE}"
      else
        echo "Failed-start cleanup refused PID ${pid}: ownership metadata did not match; preserving state." >&2
      fi
    else
      rm -f "${STATE_FILE}"
    fi
  fi
  exit "${status}"
}
trap _cleanup_failed_start EXIT

status_listener() {
  # Re-read profile in case it changed.
  LISTENER_PROFILE="${LISTENER_PROFILE:-high_ports}"
  case "${LISTENER_PROFILE}" in
    high_ports) TLS_PORT=8443; H3_PORT=8444; W2_TLS=8543; W2_H3=8544 ;;
    standard_https) TLS_PORT=443; H3_PORT=443; W2_TLS=8543; W2_H3=8544 ;;
  esac
  STATE_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/physical_validation/server/${LISTENER_PROFILE}"
  PHYS_IP="$(_state_value physical_ip)"
  if [[ -z "${PHYS_IP}" ]]; then
    PHYS_IP="$(_discover_physical_ip || true)"
  fi
  PHYS_IP="${PHYS_IP:-unavailable}"
  echo "Profile:      ${LISTENER_PROFILE}"
  echo "Physical IP:  ${PHYS_IP}"
  echo "State dir:    ${STATE_DIR}"
  local pid expected_ticks expected_config expected_exe
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  expected_ticks="$(_state_value caddy_start_ticks)"
  expected_config="$(_state_value caddy_config)"
  expected_exe="$(_state_value caddy_exe)"
  if _caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
    echo "Caddy PID:    ${pid} (running, ownership verified)"
  elif [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "Caddy PID:    ${pid} (running, ownership NOT verified)"
  else
    echo "Caddy:        not running"
  fi
  echo ""
  echo "Listeners:"
  ss -ltnp 2>/dev/null |
    grep -E ":(${TLS_PORT}|${W2_TLS})[[:space:]]" || true
  ss -lunp 2>/dev/null |
    grep -E ":(${H3_PORT}|${W2_H3})[[:space:]]" || true
  echo ""
  echo "CA certificate:"
  local ca
  ca="$(find "${STATE_DIR}" -name root.crt 2>/dev/null || true)"
  ca="${ca:-$(find "$(absolute_from_experiment runs/caddy)" -name root.crt 2>/dev/null | head -1)}"
  echo "  ${ca:-not yet generated}"
}

start_listener() {
  require_command ip
  require_command caddy
  require_command ss
  CADDY_EXE="$(command -v caddy)"
  PHYS_IP="$(_discover_physical_ip || true)"
  if [[ -z "${PHYS_IP}" ]]; then
    die "Could not determine IPv4 on ${PHYS_IF}"
  fi
  local recorded_pid recorded_ticks recorded_config recorded_exe
  recorded_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  recorded_ticks="$(_state_value caddy_start_ticks)"
  recorded_config="$(_state_value caddy_config)"
  recorded_exe="$(_state_value caddy_exe)"
  if [[ "${recorded_pid}" =~ ^[0-9]+$ ]] && kill -0 "${recorded_pid}" 2>/dev/null; then
    if _caddy_pid_matches "${recorded_pid}" "${recorded_ticks}" "${recorded_config}" "${recorded_exe}"; then
      echo "Caddy is already running (PID ${recorded_pid}, ownership verified)." >&2
    else
      echo "Refusing to start: recorded PID ${recorded_pid} is live but ownership metadata does not match." >&2
    fi
    echo "Run '$0 stop' first if you need to restart." >&2
    exit 1
  elif [[ -f "${PID_FILE}" ]]; then
    if [[ ! "${recorded_pid}" =~ ^[0-9]+$ ]]; then
      die "Refusing to replace malformed PID metadata in ${PID_FILE}; inspect it explicitly"
    fi
    echo "Removing stale listener metadata for dead PID ${recorded_pid:-unknown}." >&2
    rm -f "${PID_FILE}" "${STATE_FILE}"
  fi
  local caddyfile
  caddyfile="${STATE_DIR}/Caddyfile"
  _caddyfile >"${caddyfile}"
  _caddy_data

  caddy validate --config "${caddyfile}" --adapter caddyfile
  {
    echo "status=starting"
    echo "physical_ip=${PHYS_IP}"
    echo "tls_port=${TLS_PORT}"
    echo "h3_port=${H3_PORT}"
    echo "caddy_config=${caddyfile}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "activation_requested_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"
  START_IN_PROGRESS=1
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) starting Caddy on ${PHYS_IP}" >>"${LOG_FILE}"
  caddy run --config "${caddyfile}" --adapter caddyfile >>"${LOG_FILE}" 2>&1 &
  local caddy_pid=$!
  local caddy_start_ticks
  caddy_start_ticks="$(_process_start_ticks "${caddy_pid}")"
  echo "${caddy_pid}" >"${PID_FILE}"
  {
    echo "status=starting"
    echo "physical_ip=${PHYS_IP}"
    echo "tls_port=${TLS_PORT}"
    echo "h3_port=${H3_PORT}"
    echo "caddy_pid=${caddy_pid}"
    echo "caddy_start_ticks=${caddy_start_ticks}"
    echo "caddy_config=${caddyfile}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "activation_requested_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"
  sleep 2

  if ! _caddy_pid_matches "${caddy_pid}" "${caddy_start_ticks}" "${caddyfile}" "${CADDY_EXE}"; then
    echo "Caddy failed to start; check ${LOG_FILE}" >&2
    tail -20 "${LOG_FILE}" >&2
    exit 1
  fi

  # Locate CA cert for client handoff.
  local ca
  ca="$(find "${XDG_DATA_HOME}/caddy/pki/authorities/local" -name root.crt 2>/dev/null | head -1)"
  if [[ -n "${ca}" ]]; then
    cp "${ca}" "${STATE_DIR}/root.crt"
  fi

  {
    echo "status=running"
    echo "physical_ip=${PHYS_IP}"
    echo "tls_port=${TLS_PORT}"
    echo "h3_port=${H3_PORT}"
    echo "caddy_pid=${caddy_pid}"
    echo "caddy_start_ticks=${caddy_start_ticks}"
    echo "caddy_config=${caddyfile}"
    echo "caddy_exe=${CADDY_EXE}"
    echo "ca_cert=${STATE_DIR}/root.crt"
    echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"
  START_IN_PROGRESS=0

  echo "Caddy started (PID ${caddy_pid}) on ${PHYS_IP}"
  echo "Profile:   ${LISTENER_PROFILE}"
  echo "TLS 1.3:  https://${PHYS_IP}:${TLS_PORT}/v1"
  echo "HTTP/3:   https://${PHYS_IP}:${H3_PORT}/v1"
  echo "CA cert:  ${STATE_DIR}/root.crt"
  echo "SERVER_LISTENER_READY"
}

stop_listener() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid expected_ticks expected_config expected_exe
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    expected_ticks="$(_state_value caddy_start_ticks)"
    expected_config="$(_state_value caddy_config)"
    expected_exe="$(_state_value caddy_exe)"
    if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
      die "Refusing to remove malformed PID metadata in ${PID_FILE}; inspect it explicitly"
    elif kill -0 "${pid}" 2>/dev/null; then
      if ! _stop_verified_caddy "${pid}" "${expected_ticks}" "${expected_config}" "${expected_exe}"; then
        die "Refusing to stop PID ${pid}: executable, config, or start metadata does not match project state"
      fi
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) stopped" >>"${LOG_FILE}"
    elif ! _listener_ports_closed; then
      die "Recorded Caddy is absent but one or more project listener ports remain open"
    fi
    rm -f "${PID_FILE}" "${STATE_FILE}"
  elif [[ -f "${STATE_FILE}" ]]; then
    if ! _listener_ports_closed; then
      die "Listener state exists without a PID and project ports remain open; preserving state"
    fi
    rm -f "${STATE_FILE}"
  else
    echo "No project-owned Caddy listener is recorded."
    return 0
  fi
  echo "Caddy stopped."
}

case "${ACTION}" in
  start) start_listener ;;
  status) status_listener ;;
  stop) stop_listener ;;
  *) die "Usage: $0 {start|status|stop}" ;;
esac
