#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

QA_SAMPLES="${PRELIM_QA_SAMPLES:-16}"
SUMMARY_SAMPLES="${PRELIM_SUMMARY_SAMPLES:-8}"
REPETITIONS="${PRELIM_REPETITIONS:-1}"
PRELIM_ROOT="${PRELIM_RUNS_ROOT:-runs/preliminary}"
PROXY_HOST="${PRELIM_PROXY_HOST:-10.200.0.1}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
CLIENT_IF="${CLIENT_VETH:-llmclient0}"
CAPTURE_IF="${CLIENT_IF}"
NETWORK_MODE="${PRELIM_NETWORK_MODE:-netns}"
NETWORKS="${PRELIM_NETWORKS:-baseline rtt}"
QA_MANIFEST="${MANIFEST_PATH:-artifacts/requests_32.jsonl}"
SUMMARY_MANIFEST="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"
LIFECYCLE_DIR="$(absolute_from_experiment "${PRELIM_ROOT}")/.lifecycle"
mkdir -p "${LIFECYCLE_DIR}"
LIFECYCLE_STATE="${LIFECYCLE_DIR}/preliminary-$$.state"
NETWORK_OWNED=0
CADDY_OWNED=0
CADDY_PID=""
CADDY_START_TICKS=""
CADDY_CONFIG="${EXPERIMENT_ROOT}/configs/Caddyfile"
CADDY_EXE="$(command -v caddy || true)"
require_command ss

if [[ "${PARALLEL_WORKERS:-2}" -ne 2 ]]; then
  echo "The preliminary profile requires the two configured GPU workers." >&2
  exit 2
fi

write_lifecycle_state() {
  {
    printf 'owner_pid=%s\n' "$$"
    printf 'network_owned=%s\n' "${NETWORK_OWNED}"
    printf 'namespace=%s\n' "${NETNS}"
    printf 'host_veth=%s\n' "${HOST_IF}"
    printf 'client_veth=%s\n' "${CLIENT_IF}"
    printf 'caddy_owned=%s\n' "${CADDY_OWNED}"
    printf 'caddy_pid=%s\n' "${CADDY_PID}"
    printf 'caddy_start_ticks=%s\n' "${CADDY_START_TICKS}"
    printf 'caddy_config=%s\n' "${CADDY_CONFIG}"
  } >"${LIFECYCLE_STATE}"
}

process_start_ticks() {
  local pid="$1"
  awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true
}

caddy_pid_matches() {
  local pid="$1" expected_ticks="$2" expected_config="$3"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  [[ -n "${CADDY_EXE}" &&
    "$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)" == "$(readlink -f "${CADDY_EXE}")" ]] ||
    return 1
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

wait_for_owned_caddy_exit() {
  local pid="$1" expected_ticks="$2" expected_config="$3" attempts="$4"
  local attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    if ! caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}"; then
      # The recorded process exited and the PID may have been reused. Never
      # wait on or signal a process whose identity no longer matches.
      return 0
    fi
    sleep 0.1
  done
  return 1
}

caddy_listeners_closed() {
  local tcp_listeners udp_listeners
  if ! command -v ss >/dev/null 2>&1; then
    echo "Cannot verify Caddy listener closure because ss is unavailable." >&2
    return 1
  fi
  if ! tcp_listeners="$(ss -H -ltn 2>/dev/null)" ||
    ! udp_listeners="$(ss -H -lun 2>/dev/null)"; then
    echo "Failed to inspect TCP/UDP listeners after stopping Caddy." >&2
    return 1
  fi
  if grep -Eq ':(8443|8543)[[:space:]]' <<<"${tcp_listeners}"; then
    echo "A TCP listener remains on a project Caddy port (8443 or 8543)." >&2
    return 1
  fi
  if grep -Eq ':(8444|8544)[[:space:]]' <<<"${udp_listeners}"; then
    echo "A UDP listener remains on a project Caddy port (8444 or 8544)." >&2
    return 1
  fi
}

stop_owned_proxy() {
  [[ "${CADDY_OWNED}" -eq 1 ]] || return 0
  if ! kill -0 "${CADDY_PID}" 2>/dev/null; then
    :
  elif caddy_pid_matches "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
    kill -TERM "${CADDY_PID}" 2>/dev/null || true
    if ! wait_for_owned_caddy_exit \
      "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}" 50; then
      if caddy_pid_matches \
        "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
        kill -KILL "${CADDY_PID}" 2>/dev/null || true
      elif kill -0 "${CADDY_PID}" 2>/dev/null; then
        echo "Refusing to KILL PID ${CADDY_PID}: Caddy identity changed during shutdown." >&2
        return 1
      fi
      if ! wait_for_owned_caddy_exit \
        "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}" 20; then
        echo "Verified Caddy PID ${CADDY_PID} did not exit; preserving ownership state." >&2
        return 1
      fi
    fi
  else
    if ! kill -0 "${CADDY_PID}" 2>/dev/null; then
      CADDY_OWNED=0
      CADDY_PID=""
      CADDY_START_TICKS=""
      write_lifecycle_state
      return 0
    fi
    echo "Refusing to stop PID ${CADDY_PID}: Caddy ownership metadata no longer matches." >&2
    return 1
  fi
  if ! caddy_listeners_closed; then
    echo "Caddy process exited but listener closure could not be verified; preserving ownership state." >&2
    return 1
  fi
  CADDY_OWNED=0
  CADDY_PID=""
  CADDY_START_TICKS=""
  write_lifecycle_state
}

start_owned_proxy() {
  local caddy_root
  require_command caddy
  VLLM_SECONDARY_PORT="${VLLM_SECONDARY_PORT:-$((VLLM_PORT + 1))}"
  caddy_root="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
  mkdir -p "${caddy_root}"
  export XDG_DATA_HOME="${caddy_root}/data"
  export XDG_CONFIG_HOME="${caddy_root}/config"
  export VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT SECURE_PROXY_HOST
  SECURE_PROXY_HOST="${PROXY_HOST}"
  caddy validate --config "${CADDY_CONFIG}" --adapter caddyfile
  write_lifecycle_state
  caddy run --config "${CADDY_CONFIG}" --adapter caddyfile \
    >"${LIFECYCLE_DIR}/caddy-$$.log" 2>&1 &
  CADDY_PID=$!
  CADDY_START_TICKS="$(process_start_ticks "${CADDY_PID}")"
  CADDY_OWNED=1
  write_lifecycle_state
  sleep 1
  if ! caddy_pid_matches "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
    echo "Project Caddy failed to start; see ${LIFECYCLE_DIR}/caddy-$$.log" >&2
    exit 1
  fi
}

cleanup_owned_network() {
  [[ "${NETWORK_OWNED}" -eq 1 ]] || return 0
  if ! CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" CLIENT_VETH="${CLIENT_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1; then
    echo "Failed to remove owned ${NETNS}/${HOST_IF}; preserving lifecycle state." >&2
    return 1
  fi
  NETWORK_OWNED=0
  write_lifecycle_state
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT
  stop_owned_proxy || cleanup_failed=1
  cleanup_owned_network || cleanup_failed=1
  if [[ "${cleanup_failed}" -eq 0 ]]; then
    rm -f "${LIFECYCLE_STATE}"
  else
    echo "Cleanup incomplete; ownership metadata remains at ${LIFECYCLE_STATE}." >&2
    [[ "${status}" -ne 0 ]] || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
write_lifecycle_state

run_cell() {
  local network="$1"
  local transport="$2"
  local workload="$3"
  local manifest samples max_tokens observation root
  if [[ "${workload}" == "qa" ]]; then
    manifest="${QA_MANIFEST}"
    samples="${QA_SAMPLES}"
    max_tokens="${MAX_OUTPUT_TOKENS}"
    observation="${OBSERVATION_SECONDS}"
  else
    manifest="${SUMMARY_MANIFEST}"
    samples="${SUMMARY_SAMPLES}"
    max_tokens="${SUMMARY_MAX_OUTPUT_TOKENS:-1024}"
    observation="${SUMMARY_OBSERVATION_SECONDS:-60}"
  fi
  root="${PRELIM_ROOT}/${network}/${workload}"
  echo "=== ${network} ${transport} ${workload}: ${samples} samples x ${REPETITIONS} repetition(s) ==="
  TRANSPORT="${transport}" \
  PROFILE=pilot \
  SAMPLES_OVERRIDE="${samples}" \
  REPETITIONS_OVERRIDE="${REPETITIONS}" \
  RUNS_ROOT_OVERRIDE="${root}" \
  MANIFEST_PATH_OVERRIDE="${manifest}" \
  MAX_OUTPUT_TOKENS_OVERRIDE="${max_tokens}" \
  OBSERVATION_SECONDS_OVERRIDE="${observation}" \
  CLIENT_NETNS="${NETNS}" \
  SECURE_PROXY_HOST="${PROXY_HOST}" \
  CAPTURE_INTERFACE_OVERRIDE="${CAPTURE_IF}" \
    "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
}

for network in ${NETWORKS}; do
  if [[ "${NETWORK_MODE}" == "netns" ]]; then
    CAPTURE_IF="${CLIENT_IF}"
    stop_owned_proxy
    cleanup_owned_network
    if ip link show dev "${HOST_IF}" >/dev/null 2>&1 ||
      ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
      echo "Refusing to replace existing ${HOST_IF} or ${NETNS}; inspect and remove it explicitly." >&2
      exit 2
    fi
    # Script 11 owns and rolls back partial applies. Claim outer ownership only
    # after it reports success, so a refusal cannot reset someone else's state.
    CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" CLIENT_VETH="${CLIENT_IF}" \
      "${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"
    NETWORK_OWNED=1
    write_lifecycle_state
  elif [[ "${NETWORK_MODE}" == "loopback" ]]; then
    if [[ "${network}" != "baseline" ]]; then
      echo "Loopback mode only supports baseline on an unprivileged container." >&2
      exit 2
    fi
    NETNS=""
    HOST_IF="${CAPTURE_INTERFACE:-lo}"
    CAPTURE_IF="${HOST_IF}"
    PROXY_HOST="localhost"
  else
    echo "PRELIM_NETWORK_MODE must be netns or loopback." >&2
    exit 2
  fi
  stop_owned_proxy
  start_owned_proxy

  # Complete a balanced protocol pair for each workload before moving on.
  run_cell "${network}" tls13 qa
  run_cell "${network}" http3 qa
  run_cell "${network}" tls13 summary
  run_cell "${network}" http3 summary
done

echo "PRELIMINARY_RUN_COMPLETE"
