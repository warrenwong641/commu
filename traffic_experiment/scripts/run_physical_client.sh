#!/usr/bin/env bash
# Physical-client protocol pilot. Each attempt is append-only and contains
# exactly one measured model request plus its externally captured PCAP.
set -euo pipefail

TRANSPORT_INPUT="${1:?Usage: $0 tls|tls13|h3|http3 [standard_https|high_ports]}"
LISTENER_PROFILE="${2:-standard_https}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
export PYTHONPATH="${REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

absolute_from_repository() {
  local value="$1"
  case "${value}" in
    /*) printf '%s\n' "${value}" ;;
    *) printf '%s/%s\n' "${REPOSITORY_ROOT}" "${value}" ;;
  esac
}

count_frames() {
  local filter="$1"
  shift
  "${TSHARK_CMD}" -r "${PCAP}" "$@" -Y "${filter}" \
    -T fields -e frame.number 2>/dev/null |
    awk 'NF {count++} END {print count+0}'
}

require_positive_count() {
  local label="$1" value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ || "${value}" -le 0 ]]; then
    echo "FAIL: ${label} evidence is missing (${value:-unset})" | tee -a "${LOG}"
    exit 1
  fi
}

# --- configuration ----------------------------------------------------------
SERVER_IP="${SERVER_IP:-144.214.210.31}"
if [[ -z "${LOCAL_VLLM_API_KEY:-}" ]]; then
  echo "Set LOCAL_VLLM_API_KEY in the current process environment." >&2
  exit 2
fi
: "${MODEL:?Set MODEL to the exact served-model name recorded by the server.}"

MANIFEST_INPUT="${MANIFEST_PATH:-${MANIFEST:-traffic_experiment/artifacts/requests_32.jsonl}}"
OUTPUT_INPUT="${OUTPUT_DIR:-traffic_experiment/runs/physical_validation/client}"
CA_INPUT="${CA_CERT:-traffic_experiment/certs/caddy-root.crt}"
MANIFEST="$(absolute_from_repository "${MANIFEST_INPUT}")"
OUTPUT_BASE="$(absolute_from_repository "${OUTPUT_INPUT}")"
CA_CERT="$(absolute_from_repository "${CA_INPUT}")"
PYTHON_INPUT="${CLIENT_PYTHON:-python3}"
if [[ "${PYTHON_INPUT}" == */* ]]; then
  PYTHON_BIN="$(absolute_from_repository "${PYTHON_INPUT}")"
else
  PYTHON_BIN="${PYTHON_INPUT}"
fi
CAPTURE_IF="${CAPTURE_INTERFACE:-}"
WIRESHARK_DIR="${WIRESHARK_BIN:-/mnt/c/Program Files/Wireshark}"
TSHARK="${TSHARK:-${WIRESHARK_DIR}/tshark.exe}"
DUMPCAP="${DUMPCAP:-${WIRESHARK_DIR}/dumpcap.exe}"
MTU="${CLIENT_MTU:-1420}"

if [[ ! -s "${MANIFEST}" ]]; then
  echo "Frozen request manifest is missing or empty: ${MANIFEST}" >&2
  exit 2
fi
if [[ ! -s "${CA_CERT}" ]]; then
  echo "CA certificate is missing or empty: ${CA_CERT}" >&2
  exit 2
fi
if [[ "${PYTHON_BIN}" == */* ]]; then
  [[ -x "${PYTHON_BIN}" ]] || {
    echo "Client Python is not executable: ${PYTHON_BIN}" >&2
    exit 2
  }
elif ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Client Python was not found on PATH: ${PYTHON_BIN}" >&2
  exit 2
fi
if [[ ! "${MTU}" =~ ^[0-9]+$ || "${MTU}" -lt 576 || "${MTU}" -gt 9000 ]]; then
  echo "CLIENT_MTU must be an integer between 576 and 9000; got ${MTU}." >&2
  exit 2
fi

case "${LISTENER_PROFILE}" in
  standard_https) TLS_PORT=443; H3_PORT=443 ;;
  high_ports) TLS_PORT=8443; H3_PORT=8444 ;;
  *) echo "LISTENER_PROFILE must be standard_https or high_ports" >&2; exit 2 ;;
esac

case "${TRANSPORT_INPUT}" in
  tls | tls13)
    TRANSPORT_LABEL="tls"
    CLI_TRANSPORT="tls13"
    PORT="${TLS_PORT}"
    CAP_FILTER="tcp port ${PORT} and host ${SERVER_IP}"
    ;;
  h3 | http3)
    TRANSPORT_LABEL="h3"
    CLI_TRANSPORT="http3"
    PORT="${H3_PORT}"
    # Include TCP on the same endpoint so fallback is observable and rejectable.
    CAP_FILTER="host ${SERVER_IP} and (udp port ${PORT} or tcp port ${PORT})"
    ;;
  *)
    echo "TRANSPORT must be tls, tls13, h3, or http3" >&2
    exit 2
    ;;
esac

BASE_URL="https://${SERVER_IP}:${PORT}/v1"
HEALTH_URL="https://${SERVER_IP}:${TLS_PORT}/v1"
MANIFEST_SHA256="$(sha256sum "${MANIFEST}" | awk '{print $1}')"
CA_SHA256="$(sha256sum "${CA_CERT}" | awk '{print $1}')"
MEASUREMENT_CONFIG_SHA256="$(
  "${PYTHON_BIN}" - \
    "${BASE_URL}" "${MODEL}" "${MANIFEST_SHA256}" "${CA_SHA256}" \
    "${CLI_TRANSPORT}" "${CAP_FILTER}" "${MTU}" <<'PY'
import hashlib
import json
import sys

(
    base_url,
    model,
    manifest_sha256,
    ca_sha256,
    transport,
    capture_filter,
    mtu,
) = sys.argv[1:]
definition = {
    "backend": "local_vllm",
    "base_url": base_url,
    "model": model,
    "manifest_sha256": manifest_sha256,
    "ca_sha256": ca_sha256,
    "condition": "no_compression",
    "samples": 1,
    "repetitions": 1,
    "seed": 42,
    "temperature": 0,
    "max_output_tokens": 4096,
    "stream": True,
    "connection_mode": "cold",
    "transport": transport,
    "capture_filter": capture_filter,
    "client_mtu": int(mtu),
}
payload = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
PY
)"
ATTEMPT_ROOT="${OUTPUT_BASE}/${LISTENER_PROFILE}/${TRANSPORT_LABEL}"
mkdir -p "${ATTEMPT_ROOT}"

# A successful marker is written only after protocol, direction, timestamp,
# capture hash, and result-row binding checks all pass.
evidence="$(
  "${PYTHON_BIN}" - \
    "${ATTEMPT_ROOT}" "${CLI_TRANSPORT}" "${MODEL}" \
    "${MANIFEST_SHA256}" "${MEASUREMENT_CONFIG_SHA256}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
transport, model, manifest_sha256, config_sha256 = sys.argv[2:]
expected_http = "3" if transport == "http3" else "1.1"
for results in sorted(root.glob("attempt-*/results.jsonl")):
    marker_path = results.parent / "VALIDATION_COMPLETE"
    if not marker_path.is_file():
        continue
    marker = {}
    for line in marker_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            marker[key] = value
    if not (
        marker.get("transport") == transport
        and marker.get("model") == model
        and marker.get("manifest_sha256") == manifest_sha256
        and marker.get("measurement_config_sha256") == config_sha256
        and marker.get("negotiated_http_version") == expected_http
    ):
        continue
    for line in results.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        capture_value = row.get("capture_file")
        if not (
            row.get("completed")
            and row.get("condition") == "no_compression"
            and row.get("transport") == transport
            and row.get("model") == model
            and row.get("manifest_sha256") == manifest_sha256
            and row.get("measurement_config_sha256") == config_sha256
            and row.get("connection_mode") == "cold"
            and str(row.get("negotiated_http_version", "")) == expected_http
            and capture_value
            and row.get("capture_sha256")
        ):
            continue
        generation = row.get("generation") or {}
        if not (
            generation.get("seed") == 42
            and generation.get("temperature") == 0
            and generation.get("max_tokens") == 4096
            and generation.get("stream") is True
        ):
            continue
        capture = Path(str(capture_value))
        if not capture.is_absolute():
            capture = results.parent / capture
        if capture.is_file():
            digest = hashlib.sha256(capture.read_bytes()).hexdigest()
            if (
                digest == row["capture_sha256"]
                and marker.get("capture_sha256") == digest
                and marker.get("request_id") == str(row.get("request_id"))
            ):
                print(results)
                raise SystemExit(0)
PY
)"
if [[ -n "${evidence}" ]]; then
  echo "${TRANSPORT_LABEL}: validated evidence exists at ${evidence}; skipping (zero new requests)"
  exit 0
fi

attempt=1
while true; do
  OUTPUT_DIR="${ATTEMPT_ROOT}/$(printf 'attempt-%03d' "${attempt}")"
  if mkdir "${OUTPUT_DIR}" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
done
RESULTS="${OUTPUT_DIR}/results.jsonl"
LOG="${OUTPUT_DIR}/console.log"
PCAP="${OUTPUT_DIR}/pilot.pcapng"

# --- capture interface and tools --------------------------------------------
if [[ -z "${CAPTURE_IF}" ]]; then
  echo "CAPTURE_INTERFACE not set. Discovering..."
  for candidate in \
    "$(ip link show 2>/dev/null | grep -oP '(?<=: )[^:@]+' | grep -iE 'eth|veth|enp' | head -1)" \
    "eth0" "enp0s3"; do
    if [[ -n "${candidate}" ]]; then
      CAPTURE_IF="${candidate}"
      echo "  auto-detected: ${CAPTURE_IF}"
      break
    fi
  done
fi
if [[ -z "${CAPTURE_IF}" ]]; then
  echo "Set CAPTURE_INTERFACE explicitly; discovery found no candidate." >&2
  exit 2
fi

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
echo "CAPTURE_IF=${CAPTURE_IF} TSHARK=${TSHARK_CMD} DUMPCAP=${DUMPCAP_CMD}" |
  tee -a "${LOG}"

# --- preflight and calibration: always outside packet capture ---------------
echo "Checking authenticated TLS endpoint: ${HEALTH_URL}models" | tee -a "${LOG}"
"${PYTHON_BIN}" - "${HEALTH_URL}" "${CA_CERT}" "${MODEL}" <<'PY' 2>&1 | tee -a "${LOG}"
import os
import ssl
import sys

import httpx

base_url, ca_file, expected_model = sys.argv[1:]
context = ssl.create_default_context(cafile=ca_file)
context.minimum_version = ssl.TLSVersion.TLSv1_3
context.maximum_version = ssl.TLSVersion.TLSv1_3
response = httpx.get(
    base_url.rstrip("/") + "/models",
    headers={"Authorization": "Bearer " + os.environ["LOCAL_VLLM_API_KEY"]},
    verify=context,
    timeout=10,
)
response.raise_for_status()
models = {str(item.get("id")) for item in response.json().get("data", [])}
if expected_model not in models:
    raise SystemExit(
        f"configured MODEL {expected_model!r} is not served; available={sorted(models)!r}"
    )
print(f"Endpoint OK: {expected_model}")
PY

echo "Calibrating TCP reachability before capture on ${SERVER_IP}:${TLS_PORT}" |
  tee -a "${LOG}"
"${PYTHON_BIN}" - "${SERVER_IP}" "${TLS_PORT}" <<'PY' 2>&1 | tee -a "${LOG}"
import socket
import statistics
import sys
import time

host, port = sys.argv[1], int(sys.argv[2])
rtts = []
for _ in range(3):
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5):
            rtts.append(time.perf_counter() - started)
    except OSError:
        pass
if not rtts:
    raise SystemExit("calibration failed: no TCP connection succeeded")
print(f"calib: rtt_median={statistics.median(rtts) * 1000:.1f}ms n={len(rtts)}")
PY

# --- externally captured single request -------------------------------------
CAPTURE_PID=""
CAPTURE_STOPPED=1

stop_capture() {
  [[ "${CAPTURE_STOPPED}" -eq 0 ]] || return 0
  local pid="${CAPTURE_PID}"
  local waited=0
  local status=0
  if kill -0 "${pid}" 2>/dev/null; then
    kill -INT "${pid}" 2>/dev/null || true
  fi
  while kill -0 "${pid}" 2>/dev/null && [[ "${waited}" -lt 10 ]]; do
    sleep 0.5
    waited=$((waited + 1))
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    sleep 1
  fi
  if kill -0 "${pid}" 2>/dev/null; then
    kill -KILL "${pid}" 2>/dev/null || true
  fi
  set +e
  wait "${pid}" 2>/dev/null
  status=$?
  set -e
  CAPTURE_STOPPED=1
  CAPTURE_PID=""
  if [[ "${status}" -ne 0 ]]; then
    echo "dumpcap exited with status ${status}" | tee -a "${LOG}" >&2
    return 1
  fi
}

cleanup_capture() {
  local status=$?
  trap - EXIT
  if ! stop_capture && [[ "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup_capture EXIT

echo "=== ${TRANSPORT_LABEL} pilot: one no_compression request ===" |
  tee -a "${LOG}"
"${DUMPCAP_CMD}" -q -i "${CAPTURE_IF}" -f "${CAP_FILTER}" -w "${PCAP}" &
CAPTURE_PID=$!
CAPTURE_STOPPED=0
sleep 1
if ! kill -0 "${CAPTURE_PID}" 2>/dev/null; then
  stop_capture || true
  echo "dumpcap exited before the measured request" | tee -a "${LOG}" >&2
  exit 1
fi

# Cold mode avoids the runner's unmeasured warm-up request. Thus, after
# preflight and calibration above, this is the only request inside the PCAP.
"${PYTHON_BIN}" -m traffic_experiment.traffic_measure.cli run \
  --manifest "${MANIFEST}" \
  --output-dir "${OUTPUT_DIR}" \
  --backend local_vllm \
  --base-url "${BASE_URL}" \
  --model "${MODEL}" \
  --samples 1 \
  --repetitions 1 \
  --seed 42 \
  --temperature 0 \
  --condition no_compression \
  --max-output-tokens 4096 \
  --request-timeout-seconds 900 \
  --observation-seconds 0 \
  --transport "${CLI_TRANSPORT}" \
  --connection-mode cold \
  --tls-ca-file "${CA_CERT}" \
  --no-capture \
  --no-wait-after-request \
  2>&1 | tee -a "${LOG}"

stop_capture
trap - EXIT

# The isolated attempt must contain exactly one successful normalized row.
IFS=$'\t' read -r REQUEST_ID STARTED_AT FINISHED_AT NEGOTIATED_VERSION < <(
  "${PYTHON_BIN}" - "${RESULTS}" "${CLI_TRANSPORT}" "${MODEL}" <<'PY'
import json
import sys
from pathlib import Path

results, transport, model = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
rows = [
    json.loads(line)
    for line in results.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
if len(rows) != 1:
    raise SystemExit(f"expected exactly one result row, found {len(rows)}")
row = rows[0]
if not row.get("completed") or row.get("error"):
    raise SystemExit(f"measured request failed: {row.get('error')}")
if row.get("condition") != "no_compression":
    raise SystemExit(f"unexpected condition: {row.get('condition')!r}")
if row.get("transport") != transport:
    raise SystemExit(
        f"transport mismatch: expected {transport!r}, got {row.get('transport')!r}"
    )
if row.get("model") != model:
    raise SystemExit(f"model mismatch: expected {model!r}, got {row.get('model')!r}")
if row.get("connection_mode") != "cold":
    raise SystemExit(f"unexpected connection mode: {row.get('connection_mode')!r}")
generation = row.get("generation") or {}
expected_generation = {
    "seed": 42,
    "temperature": 0,
    "max_tokens": 4096,
    "stream": True,
}
for key, value in expected_generation.items():
    if generation.get(key) != value:
        raise SystemExit(
            f"generation mismatch for {key}: expected {value!r}, "
            f"got {generation.get(key)!r}"
        )
expected_http = "3" if transport == "http3" else "1.1"
negotiated = str(row.get("negotiated_http_version", ""))
if negotiated != expected_http:
    raise SystemExit(
        f"protocol negotiation mismatch: expected {expected_http!r}, got {negotiated!r}"
    )
required = ("request_id", "started_at_utc", "finished_at_utc")
if any(not row.get(key) for key in required):
    raise SystemExit(f"result is missing one of {required!r}")
print(
    row["request_id"],
    row["started_at_utc"],
    row["finished_at_utc"],
    negotiated,
    sep="\t",
)
PY
)

# --- strict PCAP validation --------------------------------------------------
echo "=== PCAP validation ===" | tee -a "${LOG}"
if [[ ! -s "${PCAP}" ]]; then
  echo "FAIL: PCAP is empty or missing" | tee -a "${LOG}"
  exit 1
fi
PCAP_HASH="$(sha256sum "${PCAP}" | awk '{print $1}')"
echo "capture_sha256=${PCAP_HASH}" | tee -a "${LOG}"

OFFLOAD_COUNT="$(
  count_frames "ip.len==0 and frame.len>${MTU}"
)"
if [[ "${OFFLOAD_COUNT}" -gt 0 ]]; then
  echo "FAIL: ${OFFLOAD_COUNT} offloaded frames make on-wire sizes ambiguous" |
    tee -a "${LOG}"
  exit 1
fi

max_ip="$(
  "${TSHARK_CMD}" -r "${PCAP}" -Y "ip.len>0" -T fields -e ip.len 2>/dev/null |
    awk '$1+0 > max {max=$1+0} END {print max+0}'
)"
if [[ "${max_ip}" -le 0 ]]; then
  echo "FAIL: no IPv4 packet lengths were decoded" | tee -a "${LOG}"
  exit 1
fi
if [[ "${max_ip}" -gt "${MTU}" ]]; then
  echo "FAIL: max IP packet ${max_ip} exceeds client MTU ${MTU}" | tee -a "${LOG}"
  exit 1
fi

if [[ "${CLI_TRANSPORT}" == "tls13" ]]; then
  PROTO="tcp"
  DECODE_ARGS=(-d "tcp.port==${PORT},tls")
  PROTOCOL_COUNT="$(count_frames "tcp.port==${PORT}")"
  APPLICATION_COUNT="$(count_frames "tls" "${DECODE_ARGS[@]}")"
  require_positive_count "TCP" "${PROTOCOL_COUNT}"
  require_positive_count "TLS" "${APPLICATION_COUNT}"
else
  PROTO="udp"
  DECODE_ARGS=(-d "udp.port==${PORT},quic")
  PROTOCOL_COUNT="$(count_frames "udp.port==${PORT}")"
  APPLICATION_COUNT="$(count_frames "quic" "${DECODE_ARGS[@]}")"
  TCP_FALLBACK_COUNT="$(count_frames "tcp.port==${PORT}")"
  require_positive_count "UDP" "${PROTOCOL_COUNT}"
  require_positive_count "QUIC" "${APPLICATION_COUNT}"
  if [[ "${TCP_FALLBACK_COUNT}" -ne 0 ]]; then
    echo "FAIL: HTTP/3 capture contains ${TCP_FALLBACK_COUNT} TCP fallback packets" |
      tee -a "${LOG}"
    exit 1
  fi
fi

UPLINK_COUNT="$(count_frames "${PROTO}.dstport==${PORT}")"
DOWNLINK_COUNT="$(count_frames "${PROTO}.srcport==${PORT}")"
require_positive_count "uplink" "${UPLINK_COUNT}"
require_positive_count "downlink" "${DOWNLINK_COUNT}"
echo "protocol_packets=${PROTOCOL_COUNT} application_packets=${APPLICATION_COUNT} uplink=${UPLINK_COUNT} downlink=${DOWNLINK_COUNT} max_ip=${max_ip}" |
  tee -a "${LOG}"

read -r FIRST_PACKET_EPOCH LAST_PACKET_EPOCH < <(
  "${TSHARK_CMD}" -r "${PCAP}" -T fields -e frame.time_epoch 2>/dev/null |
    awk 'NF {if (!first) first=$1; last=$1} END {print first, last}'
)
"${PYTHON_BIN}" - \
  "${STARTED_AT}" "${FINISHED_AT}" \
  "${FIRST_PACKET_EPOCH}" "${LAST_PACKET_EPOCH}" <<'PY'
import datetime as dt
import sys

started = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp()
finished = dt.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00")).timestamp()
first = float(sys.argv[3])
last = float(sys.argv[4])
tolerance = 2.0
if first < started - tolerance or last > finished + tolerance:
    raise SystemExit(
        "capture contains traffic outside the measured request window: "
        f"request=({started}, {finished}) capture=({first}, {last})"
    )
if last < started or first > finished:
    raise SystemExit("capture timestamps do not overlap the measured request")
PY

# Bind this attempt's PCAP to the dynamically discovered result row.
"${PYTHON_BIN}" -m traffic_experiment.traffic_measure.cli repair \
  --results "${RESULTS}" \
  --pcap "${PCAP}" \
  --request-id "${REQUEST_ID}" \
  --condition no_compression \
  --transport "${CLI_TRANSPORT}" \
  --capture-interface "${CAPTURE_IF}" \
  --capture-filter "${CAP_FILTER}" \
  --manifest-sha256 "${MANIFEST_SHA256}" \
  --measurement-config-sha256 "${MEASUREMENT_CONFIG_SHA256}" \
  2>&1 | tee -a "${LOG}"

"${PYTHON_BIN}" - "${RESULTS}" "${PCAP}" "${PCAP_HASH}" "${REQUEST_ID}" <<'PY'
import json
import sys
from pathlib import Path

results, pcap, expected_hash, request_id = (
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    sys.argv[3],
    sys.argv[4],
)
rows = [
    json.loads(line)
    for line in results.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
matches = [row for row in rows if str(row.get("request_id")) == request_id]
if len(matches) != 1:
    raise SystemExit(f"expected one repaired row, found {len(matches)}")
row = matches[0]
if Path(str(row.get("capture_file"))).resolve() != pcap.resolve():
    raise SystemExit("repaired capture path does not match this attempt's PCAP")
if row.get("capture_sha256") != expected_hash:
    raise SystemExit("repaired capture hash does not match validated PCAP")
if not row.get("repair_attached_pcap"):
    raise SystemExit("repair audit flag is absent")
if not row.get("external_capture") or row.get("capture_may_be_truncated"):
    raise SystemExit("external capture metadata is inconsistent")
if row.get("manifest_sha256") is None:
    raise SystemExit("repaired row is missing its frozen-manifest hash")
if row.get("measurement_config_sha256") is None:
    raise SystemExit("repaired row is missing its measurement-definition hash")
PY

{
  printf 'request_id=%s\n' "${REQUEST_ID}"
  printf 'transport=%s\n' "${CLI_TRANSPORT}"
  printf 'model=%s\n' "${MODEL}"
  printf 'manifest_sha256=%s\n' "${MANIFEST_SHA256}"
  printf 'measurement_config_sha256=%s\n' "${MEASUREMENT_CONFIG_SHA256}"
  printf 'condition=no_compression\n'
  printf 'seed=42\n'
  printf 'temperature=0\n'
  printf 'max_output_tokens=4096\n'
  printf 'connection_mode=cold\n'
  printf 'negotiated_http_version=%s\n' "${NEGOTIATED_VERSION}"
  printf 'capture_sha256=%s\n' "${PCAP_HASH}"
  printf 'validated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"${OUTPUT_DIR}/VALIDATION_COMPLETE"

echo "PHYSICAL_CLIENT_${TRANSPORT_LABEL^^}_OK attempt=${OUTPUT_DIR}" |
  tee -a "${LOG}"
