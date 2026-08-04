#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap

TRANSPORT="${TRANSPORT:-tls13}"
SESSION_TURNS="${SESSION_TURNS:-2}"
SESSION_START_INTERVAL_SECONDS="${SESSION_START_INTERVAL_SECONDS:-0}"
SESSION_BUDGET_SECONDS="${SESSION_BUDGET_SECONDS:-30}"
SESSION_CONDITION="${SESSION_CONDITION:-no_compression}"
SESSION_ID="${SESSION_ID:-warm-$(date -u +%Y%m%dT%H%M%SZ)}"
SECURE_PROXY_HOST="${SECURE_PROXY_HOST:-localhost}"
CAPTURE_INTERFACE_EFFECTIVE="${CAPTURE_INTERFACE_OVERRIDE:-${CAPTURE_INTERFACE}}"
RUN_PREFIX=()
if [[ -n "${CLIENT_NETNS:-}" ]]; then
  require_command ip
  RUN_PREFIX=(ip netns exec "${CLIENT_NETNS}")
fi
MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/sessions/${SESSION_ID}_${TRANSPORT}"
mkdir -p "${RUN_DIR}"
CADDY_RUN_DIR_ABS="$(absolute_from_experiment "${CADDY_RUN_DIR}")"
CA_FILE="${CADDY_RUN_DIR_ABS}/data/caddy/pki/authorities/local/root.crt"
if [[ ! -f "${CA_FILE}" ]]; then
  echo "Missing ${CA_FILE}; run 07_start_secure_proxy.sh first." >&2
  exit 2
fi

case "${TRANSPORT}" in
  tls13)
    BASE_URL="https://${SECURE_PROXY_HOST}:8443/v1"
    CAPTURE_FILTER_SESSION="tcp port 8443"
    TLS_ARGS=(--tls-ca-file "${CA_FILE}")
    ;;
  http3)
    BASE_URL="https://${SECURE_PROXY_HOST}:8444/v1"
    CAPTURE_FILTER_SESSION="udp port 8444"
    TLS_ARGS=(--tls-ca-file "${CA_FILE}")
    ;;
  *)
    echo "TRANSPORT must be tls13 or http3." >&2
    exit 2
    ;;
esac

BEFORE="${RUN_DIR}/vllm_before.json"
AFTER="${RUN_DIR}/vllm_after.json"
DIFF="${RUN_DIR}/vllm_session_rates.json"
PCAP="${RUN_DIR}/session.pcapng"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli server-snapshot \
  --metrics-url "http://127.0.0.1:${VLLM_PORT}/metrics" \
  --output "${BEFORE}"

"${RUN_PREFIX[@]}" dumpcap -q -i "${CAPTURE_INTERFACE_EFFECTIVE}" \
  -f "${CAPTURE_FILTER_SESSION}" -w "${PCAP}" &
CAPTURE_PID=$!
cleanup() {
  if kill -0 "${CAPTURE_PID}" 2>/dev/null; then
    kill -INT "${CAPTURE_PID}" 2>/dev/null || true
    wait "${CAPTURE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
sleep "${CAPTURE_STARTUP_DELAY_SECONDS:-1}"

"${RUN_PREFIX[@]}" "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST_ABS}" \
  --output-dir "${RUN_DIR}" \
  --backend local_vllm \
  --base-url "${BASE_URL}" \
  --model "${VLLM_SERVED_MODEL_NAME}" \
  --samples "${SESSION_TURNS}" \
  --repetitions 1 \
  --seed "${RANDOM_SEED}" \
  --temperature 0 \
  --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
  --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
  --observation-seconds 0 \
  --transport "${TRANSPORT}" \
  --connection-mode warm \
  --session-id "${SESSION_ID}" \
  --condition "${SESSION_CONDITION}" \
  --request-start-interval-seconds "${SESSION_START_INTERVAL_SECONDS}" \
  --session-budget-seconds "${SESSION_BUDGET_SECONDS}" \
  --no-capture \
  --no-wait-after-request \
  "${TLS_ARGS[@]}"

cleanup
trap - EXIT

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli server-snapshot \
  --metrics-url "http://127.0.0.1:${VLLM_PORT}/metrics" \
  --output "${AFTER}"
"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli server-diff \
  --before "${BEFORE}" \
  --after "${AFTER}" \
  --output "${DIFF}"

echo "Warm session results: ${RUN_DIR}"
