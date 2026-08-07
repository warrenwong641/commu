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

ACTION="${1:-start}"
LISTENER_PROFILE="${LISTENER_PROFILE:-high_ports}"
PHYS_IF="${PHYS_IF:-ens20f0}"
PHYS_IP="$(ip -4 -o addr show dev "${PHYS_IF}" scope global 2>/dev/null |
  awk '{print $4}' | cut -d/ -f1 | head -1)"
if [[ -z "${PHYS_IP}" ]]; then
  die "Could not determine IPv4 on ${PHYS_IF}"
fi

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

status_listener() {
  # Re-read profile in case it changed.
  LISTENER_PROFILE="${LISTENER_PROFILE:-high_ports}"
  case "${LISTENER_PROFILE}" in
    high_ports) TLS_PORT=8443; H3_PORT=8444; W2_TLS=8543; W2_H3=8544 ;;
    standard_https) TLS_PORT=443; H3_PORT=443; W2_TLS=8543; W2_H3=8544 ;;
  esac
  STATE_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/physical_validation/server/${LISTENER_PROFILE}"
  echo "Profile:      ${LISTENER_PROFILE}"
  echo "Physical IP:  ${PHYS_IP}"
  echo "State dir:    ${STATE_DIR}"
  if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "Caddy PID:    $(cat "${PID_FILE}") (running)"
  else
    echo "Caddy:        not running"
  fi
  echo ""
  echo "Listeners:"
  ss -ltnp 2>/dev/null | grep -E "${PHYS_IP}:844[3-4]|${PHYS_IP}:854[3-4]" || true
  ss -lunp 2>/dev/null | grep -E "${PHYS_IP}:844[3-4]|${PHYS_IP}:854[3-4]" || true
  echo ""
  echo "CA certificate:"
  local ca
  ca="$(find "${STATE_DIR}" -name root.crt 2>/dev/null || true)"
  ca="${ca:-$(find "$(absolute_from_experiment runs/caddy)" -name root.crt 2>/dev/null | head -1)}"
  echo "  ${ca:-not yet generated}"
}

start_listener() {
  # Clean up stale PID file from a failed previous start.
  if [[ -f "${PID_FILE}" ]] && ! kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "Removing stale PID file (process $(cat "${PID_FILE}") is dead)." >&2
    rm -f "${PID_FILE}"
  fi
  if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "Caddy is already running (PID $(cat "${PID_FILE}"))." >&2
    echo "Run '$0 stop' first if you need to restart." >&2
    exit 1
  fi
  local caddyfile
  caddyfile="${STATE_DIR}/Caddyfile"
  _caddyfile >"${caddyfile}"
  _caddy_data

  caddy validate --config "${caddyfile}" --adapter caddyfile
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) starting Caddy on ${PHYS_IP}" >>"${LOG_FILE}"
  caddy run --config "${caddyfile}" --adapter caddyfile >>"${LOG_FILE}" 2>&1 &
  local caddy_pid=$!
  echo "${caddy_pid}" >"${PID_FILE}"
  sleep 2

  if ! kill -0 "${caddy_pid}" 2>/dev/null; then
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
    echo "physical_ip=${PHYS_IP}"
    echo "tls_port=8443"
    echo "h3_port=8444"
    echo "caddy_pid=${caddy_pid}"
    echo "ca_cert=${STATE_DIR}/root.crt"
    echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"

  echo "Caddy started (PID ${caddy_pid}) on ${PHYS_IP}"
  echo "Profile:   ${LISTENER_PROFILE}"
  echo "TLS 1.3:  https://${PHYS_IP}:${TLS_PORT}/v1"
  echo "HTTP/3:   https://${PHYS_IP}:${H3_PORT}/v1"
  echo "CA cert:  ${STATE_DIR}/root.crt"
  echo "SERVER_LISTENER_READY"
}

stop_listener() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      _caddy_data
      caddy stop >>"${LOG_FILE}" 2>&1 || true
      kill "${pid}" 2>/dev/null || true
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) stopped" >>"${LOG_FILE}"
    fi
    rm -f "${PID_FILE}"
  fi
  echo "Caddy stopped."
}

case "${ACTION}" in
  start) start_listener ;;
  status) status_listener ;;
  stop) stop_listener ;;
  *) die "Usage: $0 {start|status|stop}" ;;
esac
