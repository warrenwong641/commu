#!/usr/bin/env bash
# Temporary physical-firewall helper for client-to-server validation.
# Opens ONLY TCP 8443 + UDP 8444 from the narrowest defensible source.
# Idempotent, append-only logged, deterministic rollback.
#
# Usage (requires root):
#   sudo bash scripts/25_physical_firewall.sh status    # read-only
#   sudo bash scripts/25_physical_firewall.sh apply     # open ports
#   sudo bash scripts/25_physical_firewall.sh cleanup   # remove our rules
set -euo pipefail

ACTION="${1:-status}"
LISTENER_PROFILE="${LISTENER_PROFILE:-standard_https}"
LOG_DIR="/home/wongshingyin/commu/traffic_experiment/runs/physical_validation/server/${LISTENER_PROFILE}"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/firewall.log"

case "${LISTENER_PROFILE}" in
  high_ports) TLS_PORT=8443; H3_PORT=8444 ;;
  standard_https) TLS_PORT=443; H3_PORT=443 ;;
  *) echo "LISTENER_PROFILE must be high_ports or standard_https" >&2; exit 2 ;;
esac

# Narrowest defensible source: the university subnet observed in SSH
# connections plus the VPN private range.  Never 0.0.0.0/0.
CLIENT_SRC="${CLIENT_SRC:-144.214.0.0/16}"
VPN_SRC="${VPN_SRC:-10.10.48.0/24}"

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: this script requires root (sudo)." >&2
    exit 2
  fi
}

log_msg() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "${LOG}"; }

status_fw() {
  echo "Profile: ${LISTENER_PROFILE}  (TLS ${TLS_PORT}, H3 ${H3_PORT})"
  echo "=== ufw status ==="
  ufw status verbose 2>&1 || true
  echo ""
  echo "=== iptables rules matching ${TLS_PORT}/${H3_PORT} ==="
  for chain in INPUT FORWARD; do
    iptables -L "${chain}" -n -v 2>/dev/null | grep -E "${TLS_PORT}|${H3_PORT}" || true
  done
  echo ""
  echo "Log: ${LOG}"
}

apply_fw() {
  require_root
  log_msg "apply: profile=${LISTENER_PROFILE} TCP ${TLS_PORT} + UDP ${H3_PORT} from ${CLIENT_SRC} ${VPN_SRC}"

  # Record pre-existing rules for deterministic rollback.
  ufw status numbered >"${LOG_DIR}/ufw_before.txt" 2>/dev/null || true

  for src in "${CLIENT_SRC}" "${VPN_SRC}"; do
    ufw allow proto tcp from "${src}" to any port "${TLS_PORT}" \
      comment "commu-physical-client-tls" >>"${LOG}" 2>&1 || true
    ufw allow proto udp from "${src}" to any port "${H3_PORT}" \
      comment "commu-physical-client-h3" >>"${LOG}" 2>&1 || true
  done

  log_msg "applied"
  echo ""
  ufw status verbose
}

cleanup_fw() {
  require_root
  log_msg "cleanup: removing commu-physical-client rules"

  # Remove only rules with our comment tag — preserves unrelated rules.
  local rules
  rules="$(ufw status numbered 2>/dev/null | grep 'commu-physical-client' || true)"
  if [[ -z "${rules}" ]]; then
    log_msg "no commu-physical-client rules found — nothing to remove"
    return
  fi
  # Delete in reverse order (highest number first) to avoid renumbering.
  echo "${rules}" | awk -F'[][]' '{print $2}' | sort -rn | while read -r num; do
    ufw --force delete "${num}" >>"${LOG}" 2>&1 || true
    log_msg "deleted rule ${num}"
  done
  log_msg "cleanup complete"
}

case "${ACTION}" in
  status) status_fw ;;
  apply) apply_fw ;;
  cleanup) cleanup_fw ;;
  *)
    echo "Usage: $0 {status|apply|cleanup}" >&2
    exit 2
    ;;
esac
