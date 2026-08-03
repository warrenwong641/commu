#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_value CAPTURE_INTERFACE
require_command dumpcap

TRANSPORT="${TRANSPORT:-tls13}"
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
  main) SAMPLES=32; REPETITIONS=5 ;;
  robustness) SAMPLES=32; REPETITIONS=10 ;;
  *) echo "PROFILE must be pilot, main, or robustness." >&2; exit 2 ;;
esac

CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
CA_FILE="${CADDY_RUN_DIR}/data/caddy/pki/authorities/local/root.crt"
if [[ ! -f "${CA_FILE}" ]]; then
  echo "Missing ${CA_FILE}; run 07_start_secure_proxy.sh first." >&2
  exit 2
fi

MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
RUN_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/local_vllm_${TRANSPORT}_${PROFILE}"
mkdir -p "${RUN_DIR}"

"${RUNNER_PYTHON}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST_ABS}" \
  --output-dir "${RUN_DIR}" \
  --backend local_vllm \
  --base-url "https://localhost:${PORT}/v1" \
  --model "${VLLM_SERVED_MODEL_NAME}" \
  --samples "${SAMPLES}" \
  --repetitions "${REPETITIONS}" \
  --seed "${RANDOM_SEED}" \
  --max-output-tokens "${MAX_OUTPUT_TOKENS}" \
  --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
  --observation-seconds "${OBSERVATION_SECONDS}" \
  --capture-interface "${CAPTURE_INTERFACE}" \
  --capture-filter "$(if [[ "${TRANSPORT}" == http3 ]]; then echo "udp port ${PORT}"; else echo "tcp port ${PORT}"; fi)" \
  --transport "${TRANSPORT}" \
  --connection-mode cold \
  --tls-ca-file "${CA_FILE}"
