#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
source "${SCRIPT_DIR}/protocol_admission.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: the matrix controls namespaces, qdiscs, MTU, and offloads." >&2
  exit 2
fi
require_value LOCAL_VLLM_API_KEY

QA_SAMPLES="${LAB_QA_SAMPLES:-32}"
SUMMARY_SAMPLES="${LAB_SUMMARY_SAMPLES:-20}"
REPETITIONS="${LAB_REPETITIONS:-3}"
PROXY_HOST="${SECURE_PROXY_HOST:-10.200.0.1}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
CLIENT_IF="${CLIENT_VETH:-llmclient0}"
NETWORKS="${LAB_NETWORKS:-baseline rtt realistic}"
TRANSPORTS="${LAB_TRANSPORTS:-tls13 http3}"
WORKLOADS="${LAB_WORKLOADS:-qa summary}"
CAPTURE_MAX="${CAPTURE_MAX_SECONDS:-900}"
LIFECYCLE_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/.lifecycle"
mkdir -p "${LIFECYCLE_DIR}"
LIFECYCLE_STATE="${LIFECYCLE_DIR}/lab-matrix-$$.state"
CADDY_STATE_FILE="${LIFECYCLE_DIR}/lab-matrix-$$.caddy.state"
CADDY_LOG_FILE="${LIFECYCLE_DIR}/lab-matrix-$$.caddy.log"
NETWORK_OWNED=0
CADDY_OWNED=0

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
  # The transactional network helper owns and rolls back partial failures.
  # Claim outer cleanup only after this invocation completed a successful apply.
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
  CAPTURE_INTERFACE_OVERRIDE="${CLIENT_IF}" \
    "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
}

"${SCRIPT_DIR}/17_lab_preflight.sh"
verify_protocol_admission
for network in ${NETWORKS}; do
  cleanup_resources
  claim_and_apply_network "${network}"
  LINK_CALIBRATION_LABEL="${network}" "${SCRIPT_DIR}/15_calibrate_link.sh"
  start_owned_proxy

  for workload in ${WORKLOADS}; do
    for transport in ${TRANSPORTS}; do
      run_cell "${network}" "${transport}" "${workload}"
    done
  done
done

cleanup_resources
rm -f "${LIFECYCLE_STATE}"
trap - EXIT
echo "LAB_MATRIX_COMPLETE"
