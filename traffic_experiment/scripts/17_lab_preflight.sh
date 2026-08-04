#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required=(ip tc ethtool iperf3 dumpcap tshark curl nvidia-smi caddy)
missing=()
for command_name in "${required[@]}"; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    missing+=("${command_name}")
  fi
done
if ((${#missing[@]})); then
  echo "Missing required commands: ${missing[*]}" >&2
  exit 2
fi
if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  echo "Missing runner environment: ${RUNNER_PYTHON}; run scripts/01_setup_runner.sh." >&2
  exit 2
fi
if ! "${RUNNER_PYTHON}" -c 'import aioquic, httpx, yaml' >/dev/null; then
  echo "Runner Python is missing required modules; rerun scripts/01_setup_runner.sh." >&2
  exit 2
fi
if [[ "${MAX_OUTPUT_TOKENS:-0}" -ne 4096 ]]; then
  echo "MAX_OUTPUT_TOKENS must be 4096 for the lab profile; got ${MAX_OUTPUT_TOKENS:-unset}." >&2
  exit 2
fi
if [[ "${SUMMARY_MAX_OUTPUT_TOKENS:-0}" -ne 4096 ]]; then
  echo "SUMMARY_MAX_OUTPUT_TOKENS must be 4096 for the lab profile." >&2
  exit 2
fi
if [[ "${NETWORK_MTU:-0}" -ne 1500 ]]; then
  echo "NETWORK_MTU must be 1500 for the paper-compatible primary setting." >&2
  exit 2
fi
if [[ ! -f "$(absolute_from_experiment "${MANIFEST_PATH}")" ]]; then
  echo "Missing frozen QA manifest: ${MANIFEST_PATH}" >&2
  exit 2
fi
if [[ ! -f "$(absolute_from_experiment "${SUMMARY_MANIFEST_PATH}")" ]]; then
  echo "Missing frozen summary manifest: ${SUMMARY_MANIFEST_PATH}" >&2
  exit 2
fi

AUDIT_DIR="$(absolute_from_experiment "${RUNS_ROOT}")/machine_audit"
mkdir -p "${AUDIT_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="${AUDIT_DIR}/preflight-${STAMP}.txt"
{
  echo "timestamp_utc=${STAMP}"
  echo "hostname=$(hostname)"
  echo "kernel=$(uname -srmo)"
  echo "os_release:"
  sed 's/^/  /' /etc/os-release 2>/dev/null || true
  echo "python=$("${RUNNER_PYTHON}" --version 2>&1)"
  echo "caddy=$(caddy version 2>&1)"
  echo "network_mtu=${NETWORK_MTU}"
  echo "network_rtt_ms=${NETWORK_RTT_MS}"
  echo "network_uplink_mbit=${NETWORK_UPLINK_MBIT}"
  echo "network_downlink_mbit=${NETWORK_DOWNLINK_MBIT}"
  echo "max_output_tokens=${MAX_OUTPUT_TOKENS}"
  echo "model=${VLLM_MODEL}"
  echo "model_revision=${VLLM_MODEL_REVISION:-UNPINNED}"
  echo "manifest_sha256:"
  sha256sum \
    "$(absolute_from_experiment "${MANIFEST_PATH}")" \
    "$(absolute_from_experiment "${SUMMARY_MANIFEST_PATH}")" |
    sed 's/^/  /'
  echo "gpus:"
  nvidia-smi --query-gpu=index,name,uuid,memory.total,driver_version \
    --format=csv,noheader | sed 's/^/  /'
  echo "capture_interfaces:"
  dumpcap -D 2>&1 | sed 's/^/  /'
  echo "routes:"
  ip route show | sed 's/^/  /'
} | tee "${REPORT}"

if [[ -z "${VLLM_MODEL_REVISION:-}" ]]; then
  echo "WARNING: VLLM_MODEL_REVISION is unpinned; pin it before collecting final results." >&2
fi
echo "LAB_PREFLIGHT_OK report=${REPORT}"
