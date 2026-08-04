#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap

TRANSPORT="${TRANSPORT:-tls13}"
SECURE_PROXY_HOST="${SECURE_PROXY_HOST:-localhost}"
CAPTURE_INTERFACE_EFFECTIVE="${CAPTURE_INTERFACE_OVERRIDE:-${CAPTURE_INTERFACE}}"
CONNECTION_MODE="${CONNECTION_MODE:-warm}"
RUN_PREFIX=()
if [[ -n "${CLIENT_NETNS:-}" ]]; then
  require_command ip
  RUN_PREFIX=(ip netns exec "${CLIENT_NETNS}")
fi
case "${TRANSPORT}" in
  tls13)
    PORT=8443
    require_command curl
    ;;
  http3)
    PORT=8444
    if ! "${RUNNER_PYTHON}" -c 'import aioquic' >/dev/null 2>&1; then
      echo "aioquic is absent; rerun 01_setup_runner.sh." >&2
      exit 2
    fi
    ;;
  *)
    echo "TRANSPORT must be tls13 or http3." >&2
    exit 2
    ;;
esac

case "${PROFILE}" in
  pilot) SAMPLES=8; REPETITIONS=3 ;;
  main) SAMPLES=32; REPETITIONS="${MAIN_REPETITIONS:-3}" ;;
  robustness) SAMPLES=32; REPETITIONS=10 ;;
  *) echo "PROFILE must be pilot, main, or robustness." >&2; exit 2 ;;
esac
SAMPLES="${SAMPLES_OVERRIDE:-${SAMPLES}}"
REPETITIONS="${REPETITIONS_OVERRIDE:-${REPETITIONS}}"

CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
CA_FILE="${CADDY_RUN_DIR}/data/caddy/pki/authorities/local/root.crt"
if [[ ! -f "${CA_FILE}" ]]; then
  echo "Missing ${CA_FILE}; run 07_start_secure_proxy.sh first." >&2
  exit 2
fi

MANIFEST_PATH_EFFECTIVE="${MANIFEST_PATH_OVERRIDE:-${MANIFEST_PATH}}"
RUNS_ROOT_EFFECTIVE="${RUNS_ROOT_OVERRIDE:-${RUNS_ROOT}}"
MAX_OUTPUT_TOKENS_EFFECTIVE="${MAX_OUTPUT_TOKENS_OVERRIDE:-${MAX_OUTPUT_TOKENS}}"
OBSERVATION_SECONDS_EFFECTIVE="${OBSERVATION_SECONDS_OVERRIDE:-${OBSERVATION_SECONDS}}"
CAPTURE_COMPLETION_ARGS=()
if [[ "${CAPTURE_STOP_ON_RESPONSE:-false}" == "true" ]]; then
  CAPTURE_COMPLETION_ARGS=(--capture-stop-on-response)
fi
MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH_EFFECTIVE}")"
RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT_EFFECTIVE}")/local_vllm_${TRANSPORT}_${PROFILE}"
mkdir -p "${RUN_DIR}"

"${RUN_PREFIX[@]}" "${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST_ABS}" \
  --output-dir "${RUN_DIR}" \
  --backend local_vllm \
  --base-url "https://${SECURE_PROXY_HOST}:${PORT}/v1" \
  --model "${VLLM_SERVED_MODEL_NAME}" \
  --samples "${SAMPLES}" \
  --repetitions "${REPETITIONS}" \
  --seed "${RANDOM_SEED}" \
  --max-output-tokens "${MAX_OUTPUT_TOKENS_EFFECTIVE}" \
  --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
  --observation-seconds "${OBSERVATION_SECONDS_EFFECTIVE}" \
  --capture-interface "${CAPTURE_INTERFACE_EFFECTIVE}" \
  --capture-filter "$(if [[ "${TRANSPORT}" == http3 ]]; then echo "udp port ${PORT}"; else echo "tcp port ${PORT}"; fi)" \
  "${CAPTURE_COMPLETION_ARGS[@]}" \
  --transport "${TRANSPORT}" \
  --connection-mode "${CONNECTION_MODE}" \
  --tls-ca-file "${CA_FILE}"
