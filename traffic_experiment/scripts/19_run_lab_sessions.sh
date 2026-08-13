#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
source "${SCRIPT_DIR}/protocol_admission.sh"
source "${SCRIPT_DIR}/worker_topology.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so the controlled network can be configured." >&2
  exit 2
fi
require_value LOCAL_VLLM_API_KEY
load_measured_worker_topology
RUNS_ROOT_ABS="$(absolute_from_experiment "${RUNS_ROOT}")"
ensure_worker_topology "${RUNS_ROOT_ABS}"

SESSION_PROFILE="${SESSION_PROFILE:-closed_loop_30s}"
NETWORKS="${LAB_SESSION_NETWORKS:-baseline rtt realistic}"
TRANSPORTS="${LAB_TRANSPORTS:-tls13 http3}"
CONDITIONS="${LAB_SESSION_CONDITIONS:-no_compression longllmlingua_2x longllmlingua_4x}"
REPETITIONS="${LAB_SESSION_REPETITIONS:-3}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
CLIENT_IF="${CLIENT_VETH:-llmclient0}"
PROXY_HOST="${SECURE_PROXY_HOST:-10.200.0.1}"
LIFECYCLE_DIR="${RUNS_ROOT_ABS}/.lifecycle"
mkdir -p "${LIFECYCLE_DIR}"
LIFECYCLE_STATE="${LIFECYCLE_DIR}/lab-sessions-$$.state"
CADDY_STATE_FILE="${LIFECYCLE_DIR}/lab-sessions-$$.caddy.state"
CADDY_LOG_FILE="${LIFECYCLE_DIR}/lab-sessions-$$.caddy.log"
NETWORK_OWNED=0
CADDY_OWNED=0

case "${SESSION_PROFILE}" in
  closed_loop_30s)
    TURNS="${LAB_QA_SAMPLES:-32}"
    START_INTERVAL="0"
    BUDGET="30"
    ;;
  antonio_10min)
    TURNS="10"
    START_INTERVAL="60"
    BUDGET="600"
    ;;
  *)
    echo "SESSION_PROFILE must be closed_loop_30s or antonio_10min." >&2
    exit 2
    ;;
esac

write_lifecycle_state() {
  {
    printf 'owner_pid=%s\n' "$$"
    printf 'network_owned=%s\n' "${NETWORK_OWNED}"
    printf 'namespace=%s\n' "${NETNS}"
    printf 'host_veth=%s\n' "${HOST_IF}"
    printf 'client_veth=%s\n' "${CLIENT_IF}"
    printf 'caddy_owned=%s\n' "${CADDY_OWNED}"
    printf 'caddy_state=%s\n' "${CADDY_STATE_FILE}"
  } >"${LIFECYCLE_STATE}"
}

stop_owned_proxy() {
  [[ "${CADDY_OWNED}" -eq 1 ]] || return 0
  if CADDY_RUN_DIR="${CADDY_RUN_DIR:-runs/lab/caddy}" \
    CADDY_STATE_FILE="${CADDY_STATE_FILE}" \
    CADDY_LOG_FILE="${CADDY_LOG_FILE}" \
    "${SCRIPT_DIR}/07_start_secure_proxy.sh" stop; then
    CADDY_OWNED=0
    write_lifecycle_state
    return 0
  fi
  echo "Failed to stop the verified project Caddy; preserving lifecycle state." >&2
  return 1
}

cleanup_owned_network() {
  [[ "${NETWORK_OWNED}" -eq 1 ]] || return 0
  if CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" CLIENT_VETH="${CLIENT_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1; then
    NETWORK_OWNED=0
    write_lifecycle_state
    return 0
  fi
  echo "Failed to remove owned ${NETNS}/${HOST_IF}; preserving lifecycle state." >&2
  return 1
}

cleanup_resources() {
  local failed=0
  stop_owned_proxy || failed=1
  cleanup_owned_network || failed=1
  return "${failed}"
}

cleanup_on_exit() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT
  cleanup_resources || cleanup_failed=1
  if [[ "${cleanup_failed}" -eq 0 ]]; then
    rm -f "${LIFECYCLE_STATE}"
  else
    echo "Cleanup incomplete; ownership metadata remains at ${LIFECYCLE_STATE}." >&2
    [[ "${status}" -ne 0 ]] || status=1
  fi
  exit "${status}"
}
trap cleanup_on_exit EXIT
write_lifecycle_state

claim_and_apply_network() {
  local network="$1"
  if ip link show dev "${HOST_IF}" >/dev/null 2>&1 ||
    ip link show dev "${CLIENT_IF}" >/dev/null 2>&1 ||
    ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
    echo "Refusing to replace existing ${HOST_IF}, ${CLIENT_IF}, or ${NETNS}." >&2
    return 2
  fi
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" CLIENT_VETH="${CLIENT_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"
  # The transactional helper handles partial apply failures itself. Do not let
  # this invocation reset pre-existing ownership state after a refused apply.
  NETWORK_OWNED=1
  write_lifecycle_state
}

start_owned_proxy() {
  CADDY_OWNED=1
  write_lifecycle_state
  CADDY_RUN_DIR="${CADDY_RUN_DIR:-runs/lab/caddy}" \
    CADDY_STATE_FILE="${CADDY_STATE_FILE}" \
    CADDY_LOG_FILE="${CADDY_LOG_FILE}" \
    SECURE_PROXY_HOST="${PROXY_HOST}" \
    "${SCRIPT_DIR}/07_start_secure_proxy.sh" start
  write_lifecycle_state
}

"${SCRIPT_DIR}/17_lab_preflight.sh"
verify_protocol_admission
for network in ${NETWORKS}; do
  cleanup_resources
  claim_and_apply_network "${network}"
  start_owned_proxy
  for repetition in $(seq 1 "${REPETITIONS}"); do
    for condition in ${CONDITIONS}; do
      for transport in ${TRANSPORTS}; do
        base_session_id="${SESSION_PROFILE}_${network}_${condition}_${transport}_r${repetition}"
        session_id="${base_session_id}"
        retry=0
        while true; do
          session_dir="$(absolute_from_experiment "${RUNS_ROOT}")/sessions/${session_id}_${transport}"
          if [[ -f "${session_dir}/SESSION_COMPLETE" ]]; then
            echo "Skip completed session: ${session_id}"
            session_id=""
            break
          fi
          if [[ ! -e "${session_dir}" ]]; then
            break
          fi
          retry=$((retry + 1))
          session_id="${base_session_id}_retry${retry}"
        done
        if [[ -z "${session_id}" ]]; then
          continue
        fi
        TRANSPORT="${transport}" \
        SESSION_ID="${session_id}" \
        SESSION_TURNS="${TURNS}" \
        SESSION_START_INTERVAL_SECONDS="${START_INTERVAL}" \
        SESSION_BUDGET_SECONDS="${BUDGET}" \
        SESSION_CONDITION="${condition}" \
        MAX_OUTPUT_TOKENS="4096" \
        CLIENT_NETNS="${NETNS}" \
        CAPTURE_INTERFACE_OVERRIDE="${CLIENT_IF}" \
        SECURE_PROXY_HOST="${PROXY_HOST}" \
          "${SCRIPT_DIR}/14_run_warm_session.sh"
      done
    done
  done
done

cleanup_resources
rm -f "${LIFECYCLE_STATE}"
trap - EXIT
echo "LAB_SESSIONS_COMPLETE profile=${SESSION_PROFILE}"
