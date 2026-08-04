#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command ip
require_command iperf3

NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IP="${HOST_VETH_CIDR:-10.200.0.1/24}"
HOST_IP="${HOST_IP%/*}"
DURATION="${LINK_CALIBRATION_SECONDS:-10}"
OUTPUT_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/link_calibration"
mkdir -p "${OUTPUT_DIR}"

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
  local server_pid=$!
  sleep 0.5
  ip netns exec "${NETNS}" iperf3 -c "${HOST_IP}" -p "${port}" \
    -t "${DURATION}" --json "$@" >"${OUTPUT_DIR}/${name}_client.json"
  wait "${server_pid}"
}

# Client to server is the application uplink.
run_test uplink 5201
# Reverse mode sends from the host server to the client.
run_test downlink 5202 --reverse

echo "Link calibration results: ${OUTPUT_DIR}"
