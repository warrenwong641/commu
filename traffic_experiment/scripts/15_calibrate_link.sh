#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command ip
require_command iperf3
IPERF_EXE="$(command -v iperf3)"

NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IP="${HOST_VETH_CIDR:-10.200.0.1/24}"
HOST_IP="${HOST_IP%/*}"
DURATION="${LINK_CALIBRATION_SECONDS:-10}"
CALIBRATION_LABEL="${LINK_CALIBRATION_LABEL:-unlabelled}"
OUTPUT_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/link_calibration/${CALIBRATION_LABEL}"
mkdir -p "${OUTPUT_DIR}"
IPERF_PID=""
IPERF_START_TICKS=""
IPERF_PORT=""

iperf_pid_matches() {
  local pid="$1" expected_ticks="$2" expected_port="$3"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" &&
    "${expected_port}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  local actual_exe expected_exe
  actual_exe="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
  expected_exe="$(readlink -f "${IPERF_EXE}" 2>/dev/null || true)"
  [[ "${actual_exe}" == "${expected_exe}" ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/${pid}/cmdline" || return 1
  local index saw_server=0 saw_port=0
  for ((index = 0; index < ${#argv[@]}; index++)); do
    [[ "${argv[index]}" == "-s" ]] && saw_server=1
    if [[ "${argv[index]}" == "-p" &&
      "${argv[index + 1]:-}" == "${expected_port}" ]]; then
      saw_port=1
    fi
  done
  [[ "${saw_server}" -eq 1 && "${saw_port}" -eq 1 ]]
}

wait_for_iperf_exit() {
  local attempts="$1" attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "${IPERF_PID}" 2>/dev/null ||
      [[ "$(process_state "${IPERF_PID}")" == "Z" ]]; then
      return 0
    fi
    if ! iperf_pid_matches "${IPERF_PID}" "${IPERF_START_TICKS}" "${IPERF_PORT}"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

stop_owned_iperf() {
  [[ -n "${IPERF_PID}" ]] || return 0
  if ! kill -0 "${IPERF_PID}" 2>/dev/null ||
    [[ "$(process_state "${IPERF_PID}")" == "Z" ]]; then
    :
  elif iperf_pid_matches "${IPERF_PID}" "${IPERF_START_TICKS}" "${IPERF_PORT}"; then
    kill -TERM "${IPERF_PID}" 2>/dev/null || true
    if ! wait_for_iperf_exit 30; then
      if iperf_pid_matches "${IPERF_PID}" "${IPERF_START_TICKS}" "${IPERF_PORT}"; then
        kill -KILL "${IPERF_PID}" 2>/dev/null || true
      fi
      if ! wait_for_iperf_exit 20; then
        echo "Owned iperf3 listener PID ${IPERF_PID} did not exit." >&2
        return 1
      fi
    fi
  else
    echo "Refusing to signal PID ${IPERF_PID}: iperf3 identity no longer matches." >&2
    return 1
  fi
  wait "${IPERF_PID}" 2>/dev/null || true
  IPERF_PID=""
  IPERF_START_TICKS=""
  IPERF_PORT=""
}

cleanup() {
  stop_owned_iperf
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
  echo "Missing network namespace ${NETNS}; apply a network condition first." >&2
  exit 2
fi

run_test() {
  local name="$1"
  local port="$2"
  shift 2
  iperf3 -s -1 -B "${HOST_IP}" -p "${port}" --json \
    >"${OUTPUT_DIR}/${name}_server.json" &
  IPERF_PID=$!
  IPERF_START_TICKS="$(process_start_ticks "${IPERF_PID}")"
  IPERF_PORT="${port}"
  if ! iperf_pid_matches "${IPERF_PID}" "${IPERF_START_TICKS}" "${IPERF_PORT}"; then
    echo "iperf3 server failed to start on ${HOST_IP}:${port}." >&2
    return 1
  fi
  sleep 0.5
  ip netns exec "${NETNS}" iperf3 -c "${HOST_IP}" -p "${port}" \
    -t "${DURATION}" --json "$@" >"${OUTPUT_DIR}/${name}_client.json"
  wait "${IPERF_PID}"
  IPERF_PID=""
  IPERF_START_TICKS=""
  IPERF_PORT=""
}

# Client to server is the application uplink.
run_test uplink 5201
# Reverse mode sends from the host server to the client.
run_test downlink 5202 --reverse

echo "Link calibration results: ${OUTPUT_DIR}"
