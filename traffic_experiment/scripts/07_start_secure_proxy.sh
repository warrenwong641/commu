#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command caddy
VLLM_SECONDARY_PORT="${VLLM_SECONDARY_PORT:-$((VLLM_PORT + 1))}"
export VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT
CADDYFILE="${EXPERIMENT_ROOT}/configs/Caddyfile"
CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
mkdir -p "${CADDY_RUN_DIR}"
export XDG_DATA_HOME="${CADDY_RUN_DIR}/data"
export XDG_CONFIG_HOME="${CADDY_RUN_DIR}/config"

caddy validate --config "${CADDYFILE}" --adapter caddyfile
caddy start --config "${CADDYFILE}" --adapter caddyfile

CA_FILE="${XDG_DATA_HOME}/caddy/pki/authorities/local/root.crt"
echo "GPU 0 proxy: TLS 1.3/H1 on TCP 8443; HTTP/3 on UDP 8444."
echo "GPU 1 proxy: TLS 1.3/H1 on TCP 8543; HTTP/3 on UDP 8544."
echo "CA certificate: ${CA_FILE}"
echo "Verify curl HTTP/3 support with: curl --version"
