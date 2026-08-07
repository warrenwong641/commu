#!/usr/bin/env bash
# Physical-client pilot runner — WSL / Windows path.
# Exactly one no_compression request per transport, independently resumable.
# Rerun after valid evidence makes zero duplicate requests.
#
# Usage (from WSL ~/commu):
#   bash scripts/run_physical_client.sh tls
#   bash scripts/run_physical_client.sh h3
set -euo pipefail

TRANSPORT="${1:?Usage: $0 tls|h3 [standard_https|high_ports]}"
LISTENER_PROFILE="${2:-standard_https}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"

# --- config -----------------------------------------------------------------
SERVER_IP="${SERVER_IP:-144.214.210.31}"
API_KEY="${API_KEY:?must be set to the vLLM API key (from server.lab.env LOCAL_VLLM_API_KEY)}"
MODEL="${MODEL:-Qwen/Qwen3-8B}"
MANIFEST="${MANIFEST:-artifacts/requests_32.jsonl}"
OUTPUT_BASE="${OUTPUT_DIR:-runs/physical_validation/client}"
CA_CERT="${CA_CERT:-${SCRIPT_DIR}/certs/caddy-root.crt}"
CAPTURE_IF="${CAPTURE_INTERFACE:-}"
WIRESHARK_DIR="${WIRESHARK_BIN:-/mnt/c/Program Files/Wireshark}"
TSHARK="${TSHARK:-${WIRESHARK_DIR}/tshark.exe}"
DUMPCAP="${DUMPCAP:-${WIRESHARK_DIR}/dumpcap.exe}"
MTU="${CLIENT_MTU:-1420}"

if [[ ! -f "${CA_CERT}" ]]; then
  echo "CA certificate not found: ${CA_CERT}" >&2
  echo "Copy it from the server's runs/physical_validation/server/${LISTENER_PROFILE}/root.crt" >&2
  exit 2
fi

# --- per-transport + profile config -----------------------------------------
case "${LISTENER_PROFILE}" in
  standard_https) TLS_PORT=443; H3_PORT=443 ;;
  high_ports) TLS_PORT=8443; H3_PORT=8444 ;;
  *) echo "LISTENER_PROFILE must be standard_https or high_ports" >&2; exit 2 ;;
esac

case "${TRANSPORT}" in
  tls)
    PORT="${TLS_PORT}"
    BASE_URL="https://${SERVER_IP}:${PORT}/v1"
    CAP_FILTER="tcp port ${PORT} and host ${SERVER_IP}"
    ;;
  h3)
    PORT="${H3_PORT}"
    BASE_URL="https://${SERVER_IP}:${PORT}/v1"
    CAP_FILTER="udp port ${PORT} and host ${SERVER_IP}"
    ;;
  *)
    echo "TRANSPORT must be tls or h3" >&2
    exit 2
    ;;
esac

OUTPUT_DIR="${OUTPUT_BASE}/${LISTENER_PROFILE}/${TRANSPORT}"
RESULTS="${OUTPUT_DIR}/results.jsonl"
LOG="${OUTPUT_DIR}/console.log"
mkdir -p "${OUTPUT_DIR}"

# --- resumability: skip if valid evidence exists ----------------------------
if [[ -f "${RESULTS}" ]]; then
  evidence="$(python3 -c "
import json, os
with open('${RESULTS}') as f:
    for l in f:
        l = l.strip()
        if not l: continue
        r = json.loads(l)
        if (r.get('completed') and r.get('condition') == 'no_compression'
            and r.get('transport') in ('tls13','http3')
            and r.get('capture_file') and os.path.isfile(r.get('capture_file', ''))
            and r.get('capture_sha256')):
            print('VALID')
            break
" 2>/dev/null)"
  if [[ "${evidence}" == "VALID" ]]; then
    echo "${TRANSPORT}: valid evidence exists — skipping (zero new requests)"
    exit 0
  fi
fi

# --- capture interface discovery --------------------------------------------
if [[ -z "${CAPTURE_IF}" ]]; then
  echo "CAPTURE_INTERFACE not set.  Discovering..."
  # Try WSL vEthernet interface first, then common patterns.
  for candidate in \
    "$(ip link show 2>/dev/null | grep -oP '(?<=: )[^:@]+' | grep -iE 'eth|veth|enp' | head -1)" \
    "eth0" "enp0s3"; do
    if [[ -n "${candidate}" ]]; then
      CAPTURE_IF="${candidate}"
      echo "  auto-detected: ${CAPTURE_IF}"
      break
    fi
  done
  if [[ -z "${CAPTURE_IF}" ]]; then
    echo "ERROR: Could not auto-detect capture interface." >&2
    echo "Set CAPTURE_INTERFACE in your environment or run:" >&2
    echo "  ip link show  | grep -oP '(?<=: )[^:@]+'  # list candidates" >&2
    exit 2
  fi
fi

# --- verify capture tools ---------------------------------------------------
TSHARK_CMD=""
DUMPCAP_CMD=""
if [[ -x "${TSHARK}" ]]; then
  TSHARK_CMD="${TSHARK}"
elif command -v tshark >/dev/null 2>&1; then
  TSHARK_CMD="tshark"
else
  echo "tshark not found at ${TSHARK} or on PATH" >&2
  exit 2
fi
if [[ -x "${DUMPCAP}" ]]; then
  DUMPCAP_CMD="${DUMPCAP}"
elif command -v dumpcap >/dev/null 2>&1; then
  DUMPCAP_CMD="dumpcap"
else
  echo "dumpcap not found at ${DUMPCAP} or on PATH" >&2
  exit 2
fi
echo "CAPTURE_IF=${CAPTURE_IF}  TSHARK=${TSHARK_CMD}  DUMPCAP=${DUMPCAP_CMD}"

# --- endpoint health check --------------------------------------------------
echo "Checking endpoint: ${BASE_URL}models"
python3 -c "
import httpx, ssl
ctx = ssl.create_default_context(cafile='${CA_CERT}')
ctx.minimum_version = ssl.TLSVersion.TLSv1_3
ctx.maximum_version = ssl.TLSVersion.TLSv1_3
r = httpx.get('${BASE_URL}models', verify=ctx, timeout=10)
r.raise_for_status()
print('Endpoint OK:', r.json()['data'][0]['id'])
" 2>&1 | tee -a "${LOG}"

# --- single-request pilot ---------------------------------------------------
# iperf3 is not required; omitting calibration in favor of Python link probe.
echo "=== ${TRANSPORT} pilot: 1 x no_compression ===" | tee -a "${LOG}"

PCAP="${OUTPUT_DIR}/pilot.pcapng"
rm -f "${PCAP}"

# --- Five-stage dumpcap lifecycle (Windows/WSL interop) --------------------
# Stage 1: SIGINT  → dumpcap flushes the pcapng and exits cleanly.
# Stage 2: poll 5s → if still alive (orphaned child), escalate.
# Stage 3: SIGTERM → harder kill.
# Stage 4: SIGKILL → absolute last resort.
# Stage 5: child-process sweep → Windows /init orphans that ignore parent signals.
# Hash is only computed AFTER the process is confirmed gone.
_stage1_cleanup() {
  if [[ -z "${CAPTURE_PID:-}" ]] || ! kill -0 "${CAPTURE_PID}" 2>/dev/null; then
    return 0
  fi
  # Stage 1: graceful flush
  kill -INT "${CAPTURE_PID}" 2>/dev/null || true
  local waited=0
  while [[ ${waited} -lt 5 ]]; do
    if ! kill -0 "${CAPTURE_PID}" 2>/dev/null; then break; fi
    sleep 0.5
    waited=$((waited + 1))
  done
  # Stage 3: TERM
  if kill -0 "${CAPTURE_PID}" 2>/dev/null; then
    kill -TERM "${CAPTURE_PID}" 2>/dev/null || true
    wait "${CAPTURE_PID}" 2>/dev/null || true
  fi
  # Stage 4: KILL
  if kill -0 "${CAPTURE_PID}" 2>/dev/null; then
    kill -KILL "${CAPTURE_PID}" 2>/dev/null || true
    wait "${CAPTURE_PID}" 2>/dev/null || true
  fi
  # Stage 5: orphan sweep (Windows /init interop — child processes may
  # have been reparented and ignore the parent's signals).
  for orphan in $(pgrep -P "${CAPTURE_PID}" 2>/dev/null || true); do
    kill -KILL "${orphan}" 2>/dev/null || true
  done
}
trap _stage1_cleanup EXIT
ERR cleanup flag for set -e failures
trap '_stage1_cleanup' ERR

# Start capture (background)
"${DUMPCAP_CMD}" -q -i "${CAPTURE_IF}" -f "${CAP_FILTER}" -w "${PCAP}" &
CAPTURE_PID=$!
sleep 1

# Python calibration (small controlled probe, no iperf3 dependency)
python3 -c "
import socket, time, statistics
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
rtts = []
for _ in range(3):
    start = time.perf_counter()
    try:
        s.connect(('${SERVER_IP}', ${PORT}))
        s.close()
        rtts.append(time.perf_counter() - start)
    except:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
if rtts:
    print(f'calib: rtt_median={statistics.median(rtts)*1000:.1f}ms n={len(rtts)}')
" 2>&1 | tee -a "${LOG}"

# Run request (non-capturing — capture is external)
python3 -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST}" \
  --output-dir "${OUTPUT_DIR}" \
  --backend local_vllm \
  --base-url "${BASE_URL}" \
  --model "${MODEL}" \
  --api-key "${API_KEY}" \
  --samples 1 \
  --repetitions 1 \
  --seed 42 \
  --condition no_compression \
  --max-output-tokens 4096 \
  --request-timeout-seconds 900 \
  --observation-seconds 0 \
  --transport "${TRANSPORT}" \
  --connection-mode warm \
  --tls-ca-file "${CA_CERT}" \
  --no-capture \
  2>&1 | tee -a "${LOG}"

_stage1_cleanup
trap - EXIT ERR

# --- validate PCAP ----------------------------------------------------------
echo "=== PCAP validation ===" | tee -a "${LOG}"
if [[ ! -s "${PCAP}" ]]; then
  echo "FAIL: PCAP is empty or missing" | tee -a "${LOG}"
  exit 1
fi
PCAP_HASH="$(sha256sum "${PCAP}" | awk '{print $1}')"
echo "capture_sha256=${PCAP_HASH}" | tee -a "${LOG}"

# Offload detection: frames with ip.len==0 but frame.len>MTU indicate
# TSO/GSO/LRO offloading.  The frame contains multiple IP segments that
# were never on the wire as a single frame.  Flag and exclude.
OFFLOAD_COUNT="$("${TSHARK_CMD}" -r "${PCAP}" -Y "ip.len==0 and frame.len>${MTU}" -T fields -e frame.number 2>/dev/null | wc -l)"
if [[ "${OFFLOAD_COUNT}" -gt 0 ]]; then
  echo "WARNING: ${OFFLOAD_COUNT} offloaded frames detected (ip.len=0, frame.len>${MTU})" | tee -a "${LOG}"
  echo "These are TSO/GSO/LRO artifacts — not on-wire frame sizes" | tee -a "${LOG}"
fi

# MTU: use IP-layer length, exclude offloaded frames.
max_ip="$("${TSHARK_CMD}" -r "${PCAP}" -Y "ip.len>0" -T fields -e ip.len -e ipv6.plen 2>/dev/null |
  awk '{
     if ($1+0>0) { v=$1+0 }
     else if ($2+0>0) { v=$2+0+40 }
     else { next }
     if (v>max) max=v
  } END { print max+0 }')"
max_frame="$("${TSHARK_CMD}" -r "${PCAP}" -Y "ip.len>0" -T fields -e frame.len 2>/dev/null | sort -n | tail -1)"
echo "max_ip_len=${max_ip}  max_frame_len(on-wire)=${max_frame}  offloaded_frames=${OFFLOAD_COUNT}" | tee -a "${LOG}"
if [[ -n "${max_ip}" && "${max_ip}" -gt "${MTU}" ]]; then
  echo "FAIL: max IP packet ${max_ip} > client MTU ${MTU}" | tee -a "${LOG}"
  exit 1
fi

case "${TRANSPORT}" in
  tls)
    proto="tcp"; decode_args=(-d "tcp.port==${PORT},tls")
    echo "TCP packets: $("${TSHARK_CMD}" -r "${PCAP}" -Y "tcp" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"
    echo "TLS records: $("${TSHARK_CMD}" -r "${PCAP}" "${decode_args[@]}" -Y "tls" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"
    ;;
  h3)
    proto="udp"; decode_args=(-d "udp.port==${PORT},quic")
    tcp_count="$("${TSHARK_CMD}" -r "${PCAP}" -Y "tcp" -T fields -e frame.number 2>/dev/null | wc -l)"
    if [[ "${tcp_count}" -gt 0 ]]; then
      echo "FAIL: HTTP/3 PCAP contains ${tcp_count} TCP packets" | tee -a "${LOG}"
      exit 1
    fi
    echo "tcp_fallback=0 (OK)" | tee -a "${LOG}"
    echo "UDP packets: $("${TSHARK_CMD}" -r "${PCAP}" -Y "udp" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"
    echo "QUIC packets: $("${TSHARK_CMD}" -r "${PCAP}" "${decode_args[@]}" -Y "quic" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"
    ;;
esac

echo "uplink: $("${TSHARK_CMD}" -r "${PCAP}" -Y "${proto}.dstport==${PORT}" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"
echo "downlink: $("${TSHARK_CMD}" -r "${PCAP}" -Y "${proto}.srcport==${PORT}" -T fields -e frame.number 2>/dev/null | wc -l)" | tee -a "${LOG}"

# --- bind PCAP to results row (repair-in-place) -----------------------------
if [[ -f "${RESULTS}" ]]; then
  python3 -c "
from traffic_measure.cli import _repair_results
from pathlib import Path
rc = _repair_results(
    results_path=Path('${RESULTS}'),
    pcap_path=Path('${PCAP}'),
    request_id='conv-30::77::no_compression',
    condition='no_compression',
    transport='${TRANSPORT}',
)
if rc == 0:
    print('pcap_bound: ${PCAP_HASH}')
else:
    print('WARNING: repair returned ' + str(rc))
" 2>&1 | tee -a "${LOG}"
fi

echo "PHYSICAL_CLIENT_${TRANSPORT^^}_OK" | tee -a "${LOG}"
