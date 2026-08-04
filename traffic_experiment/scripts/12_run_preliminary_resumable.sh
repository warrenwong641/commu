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
QA_MANIFEST="${MANIFEST_PATH:-artifacts/requests_32.jsonl}"
SUMMARY_MANIFEST="${SUMMARY_MANIFEST_PATH:-artifacts/event_summaries_10.jsonl}"

if [[ "${PARALLEL_WORKERS:-2}" -ne 2 ]]; then
  echo "The preliminary profile requires the two configured GPU workers." >&2
  exit 2
fi

stop_proxy() {
  local caddy_root
  caddy_root="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
  XDG_DATA_HOME="${caddy_root}/data" \
  XDG_CONFIG_HOME="${caddy_root}/config" \
    caddy stop >/dev/null 2>&1 || true
}

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
  CAPTURE_INTERFACE_OVERRIDE="${HOST_IF}" \
    "${SCRIPT_DIR}/08_run_transport_profile_parallel.sh"
}

for network in baseline rtt; do
  "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1 || true
  CLIENT_NETNS="${NETNS}" HOST_VETH="${HOST_IF}" \
    "${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"
  stop_proxy
  SECURE_PROXY_HOST="${PROXY_HOST}" "${SCRIPT_DIR}/07_start_secure_proxy.sh"

  # Complete a balanced protocol pair for each workload before moving on.
  run_cell "${network}" tls13 qa
  run_cell "${network}" http3 qa
  run_cell "${network}" tls13 summary
  run_cell "${network}" http3 summary
done

echo "PRELIMINARY_RUN_COMPLETE"
