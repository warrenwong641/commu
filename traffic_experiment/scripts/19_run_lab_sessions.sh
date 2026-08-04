#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root so the controlled network can be configured." >&2
  exit 2
fi

SESSION_PROFILE="${SESSION_PROFILE:-closed_loop_30s}"
NETWORKS="${LAB_SESSION_NETWORKS:-baseline rtt realistic}"
TRANSPORTS="${LAB_TRANSPORTS:-tls13 http3}"
CONDITIONS="${LAB_SESSION_CONDITIONS:-no_compression longllmlingua_2x longllmlingua_4x}"
REPETITIONS="${LAB_SESSION_REPETITIONS:-3}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
PROXY_HOST="${SECURE_PROXY_HOST:-10.200.0.1}"

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

stop_proxy() {
  local root
  root="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/lab/caddy}")"
  XDG_DATA_HOME="${root}/data" XDG_CONFIG_HOME="${root}/config" \
    caddy stop >/dev/null 2>&1 || true
}
cleanup() {
  stop_proxy
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${SCRIPT_DIR}/17_lab_preflight.sh"
for network in ${NETWORKS}; do
  cleanup
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"
  stop_proxy
  SECURE_PROXY_HOST="${PROXY_HOST}" "${SCRIPT_DIR}/07_start_secure_proxy.sh"
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
        CAPTURE_INTERFACE_OVERRIDE="${HOST_IF}" \
        SECURE_PROXY_HOST="${PROXY_HOST}" \
          "${SCRIPT_DIR}/14_run_warm_session.sh"
      done
    done
  done
done

trap - EXIT
cleanup
echo "LAB_SESSIONS_COMPLETE profile=${SESSION_PROFILE}"
