#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_worker_topology
load_worker_gpu_identities

required=(ip ss tc ethtool iperf3 dumpcap tshark curl nvidia-smi caddy sha256sum)
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
if [[ ! "${VLLM_MODEL_REVISION:-}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "VLLM_MODEL_REVISION must be an exact 40-character Hugging Face commit SHA." >&2
  exit 2
fi
QA_MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH}")"
SUMMARY_MANIFEST_ABS="$(absolute_from_experiment "${SUMMARY_MANIFEST_PATH}")"
if [[ ! -f "${QA_MANIFEST_ABS}" ]]; then
  echo "Missing frozen QA manifest: ${MANIFEST_PATH}" >&2
  exit 2
fi
if [[ ! -f "${SUMMARY_MANIFEST_ABS}" ]]; then
  echo "Missing frozen summary manifest: ${SUMMARY_MANIFEST_PATH}" >&2
  exit 2
fi
if [[ ! "${MANIFEST_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "MANIFEST_SHA256 must be the expected 64-character SHA-256 of the frozen QA manifest." >&2
  exit 2
fi
if [[ ! "${SUMMARY_MANIFEST_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "SUMMARY_MANIFEST_SHA256 must be the expected 64-character SHA-256 of the frozen summary manifest." >&2
  exit 2
fi
QA_MANIFEST_ACTUAL_SHA="$(sha256sum "${QA_MANIFEST_ABS}" | awk '{print $1}')"
SUMMARY_MANIFEST_ACTUAL_SHA="$(sha256sum "${SUMMARY_MANIFEST_ABS}" | awk '{print $1}')"
if [[ "${QA_MANIFEST_ACTUAL_SHA}" != "${MANIFEST_SHA256,,}" ]]; then
  echo "Frozen QA manifest digest mismatch: expected ${MANIFEST_SHA256,,}, got ${QA_MANIFEST_ACTUAL_SHA}." >&2
  exit 2
fi
if [[ "${SUMMARY_MANIFEST_ACTUAL_SHA}" != "${SUMMARY_MANIFEST_SHA256,,}" ]]; then
  echo "Frozen summary manifest digest mismatch: expected ${SUMMARY_MANIFEST_SHA256,,}, got ${SUMMARY_MANIFEST_ACTUAL_SHA}." >&2
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
  echo "model_revision=${VLLM_MODEL_REVISION}"
  echo "worker_count=${TOPOLOGY_WORKER_COUNT}"
  echo "worker_gpu_ids=${TOPOLOGY_GPU_IDS}"
  echo "worker_gpu_uuids=${TOPOLOGY_GPU_UUIDS}"
  echo "worker_topology:"
  for ((worker = 0; worker < TOPOLOGY_WORKER_COUNT; worker++)); do
    printf '  worker=%s physical_gpu=%s uuid=%s vllm_port=%s\n' \
      "${worker}" "${WORKER_GPU_IDS[worker]}" "${WORKER_GPU_UUIDS[worker]}" \
      "${WORKER_VLLM_PORTS[worker]}"
  done
  echo "manifest_sha256:"
  printf '  %s  %s\n' "${QA_MANIFEST_ACTUAL_SHA}" "${QA_MANIFEST_ABS}"
  printf '  %s  %s\n' "${SUMMARY_MANIFEST_ACTUAL_SHA}" "${SUMMARY_MANIFEST_ABS}"
  echo "gpus:"
  nvidia-smi --query-gpu=index,name,uuid,memory.total,driver_version \
    --format=csv,noheader | sed 's/^/  /'
  echo "capture_interfaces:"
  dumpcap -D 2>&1 | sed 's/^/  /'
  echo "routes:"
  ip route show | sed 's/^/  /'
} | tee "${REPORT}"

echo "LAB_PREFLIGHT_OK report=${REPORT}"
