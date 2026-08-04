#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: the matrix controls namespaces, qdiscs, MTU, and offloads." >&2
  exit 2
fi

QA_SAMPLES="${LAB_QA_SAMPLES:-32}"
SUMMARY_SAMPLES="${LAB_SUMMARY_SAMPLES:-20}"
REPETITIONS="${LAB_REPETITIONS:-3}"
PROXY_HOST="${SECURE_PROXY_HOST:-10.200.0.1}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
NETWORKS="${LAB_NETWORKS:-baseline rtt realistic}"
TRANSPORTS="${LAB_TRANSPORTS:-tls13 http3}"
WORKLOADS="${LAB_WORKLOADS:-qa summary}"
CAPTURE_MAX="${CAPTURE_MAX_SECONDS:-900}"

stop_proxy() {
  local caddy_root
  caddy_root="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/lab/caddy}")"
  XDG_DATA_HOME="${caddy_root}/data" \
  XDG_CONFIG_HOME="${caddy_root}/config" \
    caddy stop >/dev/null 2>&1 || true
}
cleanup() {
  stop_proxy
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_cell() {
  local network="$1"
  local transport="$2"
  local workload="$3"
  local manifest samples output_root
  if [[ "${workload}" == "qa" ]]; then
    manifest="${MANIFEST_PATH}"
    samples="${QA_SAMPLES}"
  else
    manifest="${SUMMARY_MANIFEST_PATH}"
    samples="${SUMMARY_SAMPLES}"
  fi
  output_root="${RUNS_ROOT}/matrix/${network}/${workload}"
  echo "=== ${network}/${transport}/${workload}: ${samples} samples x ${REPETITIONS} ==="
  TRANSPORT="${transport}" \
  PROFILE=main \
  SAMPLES_OVERRIDE="${samples}" \
  REPETITIONS_OVERRIDE="${REPETITIONS}" \
  RUNS_ROOT_OVERRIDE="${output_root}" \
  MANIFEST_PATH_OVERRIDE="${manifest}" \
  MAX_OUTPUT_TOKENS_OVERRIDE="4096" \
  OBSERVATION_SECONDS_OVERRIDE="${CAPTURE_MAX}" \
  CAPTURE_STOP_ON_RESPONSE="true" \
  CLIENT_NETNS="${NETNS}" \
  SECURE_PROXY_HOST="${PROXY_HOST}" \
  CAPTURE_INTERFACE_OVERRIDE="${HOST_IF}" \
    "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
}

"${SCRIPT_DIR}/17_lab_preflight.sh"
for network in ${NETWORKS}; do
  cleanup
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"
  LINK_CALIBRATION_LABEL="${network}" "${SCRIPT_DIR}/15_calibrate_link.sh"
  stop_proxy
  SECURE_PROXY_HOST="${PROXY_HOST}" "${SCRIPT_DIR}/07_start_secure_proxy.sh"

  for workload in ${WORKLOADS}; do
    for transport in ${TRANSPORTS}; do
      run_cell "${network}" "${transport}" "${workload}"
    done
  done
done

trap - EXIT
cleanup
echo "LAB_MATRIX_COMPLETE"
