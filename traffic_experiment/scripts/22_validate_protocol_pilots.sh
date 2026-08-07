#!/usr/bin/env bash
# Protocol validation pilots: exactly one QA request over TLS 1.3 and one
# over genuine HTTP/3/QUIC, with full PCAP validation, before the full matrix.
#
# Requires root (namespaces, veth, offload, tc).  Sources server.lab.env
# through lib.sh.  Append-only; never regenerates manifests or deletes partial
# runs.  Stops after both validations pass — does NOT launch the full matrix.
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# --- helpers ---------------------------------------------------------------
die() { printf '%s\n' "$*" >&2; exit 1; }
require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "This script requires root; run with sudo --preserve-env=PATH"
  fi
}
_safe_dir() {
  # Return a directory path that does not collide with existing partial runs.
  local base="$1"
  if [[ ! -d "${base}" ]]; then
    printf '%s\n' "${base}"
    return
  fi
  if find "${base}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then
    local n=2
    while [[ -d "${base}_retry${n}" ]]; do
      n=$((n + 1))
    done
    printf '%s\n' "${base}_retry${n}"
  else
    printf '%s\n' "${base}"
  fi
}

# --- validation helpers ----------------------------------------------------
validate_pcap() {
  local pcap="$1" transport="$2" port="$3"
  if [[ ! -s "${pcap}" ]]; then
    die "PCAP ${pcap} is missing or empty"
  fi
  local cap_hash
  cap_hash="$(sha256sum "${pcap}" | awk '{print $1}')"
  echo "  capture_sha256=${cap_hash}"

  # tshark decode-as args for non-standard ports.
  local decode_args=()
  case "${transport}" in
    tls13) decode_args=(-d "tcp.port==${port},tls") ;;
    http3) decode_args=(-d "udp.port==${port},quic") ;;
  esac

  # Transport-layer check
  local proto_filter
  case "${transport}" in
    tls13) proto_filter="tcp" ;;
    http3) proto_filter="udp" ;;
  esac
  local proto_count
  proto_count="$(tshark -r "${pcap}" "${decode_args[@]}" -Y "${proto_filter}" -T fields -e frame.number 2>/dev/null | wc -l)"
  if [[ "${proto_count}" -eq 0 ]]; then
    die "FAIL: no ${proto_filter} packets in ${pcap}"
  fi
  echo "  ${proto_filter}_packets=${proto_count}"

  # Wrong-protocol check (no TCP in HTTP3, no UDP in TLS)
  if [[ "${transport}" == "http3" ]]; then
    local tcp_count
    tcp_count="$(tshark -r "${pcap}" -Y "tcp" -T fields -e frame.number 2>/dev/null | wc -l)"
    if [[ "${tcp_count}" -gt 0 ]]; then
      die "FAIL: HTTP/3 PCAP contains ${tcp_count} TCP packets — fallback detected"
    fi
    echo "  tcp_fallback_packets=0 (OK)"
    # QUIC check — needs decode-as on custom UDP port
    local quic_count
    quic_count="$(tshark -r "${pcap}" "${decode_args[@]}" -Y "quic" -T fields -e frame.number 2>/dev/null | wc -l)"
    echo "  quic_packets=${quic_count}"
    if [[ "${quic_count}" -eq 0 ]]; then
      die "FAIL: no QUIC packets in HTTP/3 PCAP"
    fi
  fi

  if [[ "${transport}" == "tls13" ]]; then
    local tls_count
    tls_count="$(tshark -r "${pcap}" "${decode_args[@]}" -Y "tls" -T fields -e frame.number 2>/dev/null | wc -l)"
    echo "  tls_packets=${tls_count}"
    if [[ "${tls_count}" -eq 0 ]]; then
      die "FAIL: no TLS records in TLS PCAP"
    fi
  fi

  if [[ "${transport}" == "tls13" ]]; then
    local tls_count
    tls_count="$(tshark -r "${pcap}" -Y "tls" -T fields -e frame.number 2>/dev/null | wc -l)"
    echo "  tls_packets=${tls_count}"
    if [[ "${tls_count}" -eq 0 ]]; then
      die "FAIL: no TLS records in TLS PCAP"
    fi
  fi

  # Direction counts
  local up_count down_count
  up_count="$(tshark -r "${pcap}" -Y "${proto_filter}.dstport == ${port}" -T fields -e frame.number 2>/dev/null | wc -l)"
  down_count="$(tshark -r "${pcap}" -Y "${proto_filter}.srcport == ${port}" -T fields -e frame.number 2>/dev/null | wc -l)"
  echo "  uplink_packets=${up_count}  downlink_packets=${down_count}"
  if [[ "${up_count}" -eq 0 || "${down_count}" -eq 0 ]]; then
    die "FAIL: PCAP missing direction traffic (up=${up_count} down=${down_count})"
  fi

  # MTU check — use IP-layer length, not Ethernet frame.len.
  # Network MTU 1500 constrains the IP packet; a 1514-byte untagged
  # Ethernet frame (14-byte header + 1500-byte IP payload) is valid.
  local max_frame max_ip
  max_frame="$(tshark -r "${pcap}" -T fields -e frame.len 2>/dev/null | sort -n | tail -1)"
  max_ip="$(tshark -r "${pcap}" -T fields -e ip.len -e ipv6.plen 2>/dev/null |
    awk '{
       if ($1+0>0) { v=$1+0 }
       else if ($2+0>0) { v=$2+0+40 }
       else { next }
       if (v>max) max=v
    } END { print max+0 }')"
  echo "  max_frame_len=${max_frame}  max_ip_len=${max_ip}"
  if [[ -n "${max_ip}" && "${max_ip}" -gt "${NETWORK_MTU:-1500}" ]]; then
    die "FAIL: max IP packet ${max_ip} > configured MTU ${NETWORK_MTU:-1500}"
  fi

  # Unrelated traffic check: only port ${port} traffic should exist
  local other_count
  other_count="$(tshark -r "${pcap}" -Y "tcp || udp" -T fields -e frame.number 2>/dev/null | wc -l)"
  local port_count
  port_count="$(tshark -r "${pcap}" -Y "${proto_filter}.port == ${port}" -T fields -e frame.number 2>/dev/null | wc -l)"
  if [[ "${other_count}" -gt "${port_count}" ]]; then
    die "FAIL: PCAP contains unrelated traffic (${other_count} total vs ${port_count} on port ${port})"
  fi
  echo "  capture_filter_clean (OK)"
}

# --- main ------------------------------------------------------------------
require_root

# 1. Preflight
echo "=== Preflight ==="
EXPERIMENT_ENV_FILE="${ENV_FILE}" bash "${SCRIPT_DIR}/17_lab_preflight.sh" || die "preflight failed"

# 2. Apply the exact full-matrix baseline topology.
echo "=== Network: baseline apply ==="
CLIENT_NETNS="${CLIENT_NETNS:-llm-client}" \
  HOST_VETH="${HOST_VETH:-llmhost0}" \
  NETWORK_MTU="${NETWORK_MTU:-1500}" \
  bash "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1 || true
CLIENT_NETNS="${CLIENT_NETNS:-llm-client}" \
  HOST_VETH="${HOST_VETH:-llmhost0}" \
  NETWORK_MTU="${NETWORK_MTU:-1500}" \
  bash "${SCRIPT_DIR}/11_network_condition.sh" apply baseline

# 3. Calibrate the veth link (outside capture window)
echo "=== Link calibration (baseline veth) ==="
LINK_CALIBRATION_LABEL="protocol_validation" \
  bash "${SCRIPT_DIR}/15_calibrate_link.sh"

# 4. Start Caddy on the host veth IP (10.200.0.1).  Port 80 disabled
#    because nginx owns it.
echo "=== Caddy start (auto_https disabled, on ${SECURE_PROXY_HOST:-10.200.0.1}) ==="
pkill -f "caddy run" 2>/dev/null || true
sleep 1
# Build a temporary Caddyfile from the repo config, adding the
# auto_https disable_redirects directive so Caddy never touches :80.
CADDYFILE_TMP="$(mktemp /tmp/commu_validation_caddy.XXXXXX)"
trap 'rm -f "${CADDYFILE_TMP}"' RETURN
{
  echo '{'
  echo '  auto_https disable_redirects'
  sed -n '/servers/,/^}/p' "${EXPERIMENT_ROOT}/configs/Caddyfile"
  echo ''
  sed -n '/^https:\/\//,$ p' "${EXPERIMENT_ROOT}/configs/Caddyfile"
} >"${CADDYFILE_TMP}"
# Use host veth IP from server.lab.env so client in namespace can connect.
export VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT SECURE_PROXY_HOST
CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
mkdir -p "${CADDY_RUN_DIR}"
export XDG_DATA_HOME="${CADDY_RUN_DIR}/data"
export XDG_CONFIG_HOME="${CADDY_RUN_DIR}/config"
caddy validate --config "${CADDYFILE_TMP}" --adapter caddyfile
caddy start --config "${CADDYFILE_TMP}" --adapter caddyfile
echo "Caddy started with auto_https disabled."
trap 'caddy stop 2>/dev/null; rm -f "${CADDYFILE_TMP}"' EXIT

VALIDATION_ROOT="$(absolute_from_experiment "${RUNS_ROOT}")/protocol_validation"
mkdir -p "${VALIDATION_ROOT}"

# --- resumable single-request pilot helper ----------------------------------
# Finds the newest existing retry directory that contains a completed
# no_compression row with a valid capture.  If found, skips transport
# execution and only validates the PCAP.  Otherwise runs exactly one
# no_compression request with the given transport.
run_or_validate() {
  local transport="$1" port="$2" label="$3"
  local base_dir
  base_dir="$(_safe_dir "${VALIDATION_ROOT}/${label}")"
  mkdir -p "${base_dir}"

  # Scan existing retry dirs for a completed no_compression row.
  # The run directory may use any PROFILE suffix (pilot/main), so match
  # local_vllm_${transport}_* rather than a hardcoded _pilot.
  local existing evidence_row
  existing=""
  for d in $(ls -dt "${VALIDATION_ROOT}/${label}"_retry* 2>/dev/null || true) \
           $(ls -dt "${VALIDATION_ROOT}/${label}" 2>/dev/null || true); do
    for rf in "${d}"/local_vllm_"${transport}"_*/results.jsonl; do
      if [[ ! -f "${rf}" ]]; then continue; fi
      evidence_row="$(python3 -c "
import json
with open('${rf}') as f:
    for l in f:
        l = l.strip()
        if not l: continue
        r = json.loads(l)
        if (r.get('completed') and r.get('condition') == 'no_compression'
            and r.get('capture_file') and r.get('capture_sha256')):
            print(r['capture_file'])
            break
" 2>/dev/null)"
      if [[ -n "${evidence_row}" && -f "${evidence_row}" ]]; then
        existing="${rf}"
        break 2
      fi
    done
  done

  if [[ -n "${existing}" ]]; then
    echo "${label}: valid evidence found in ${existing} — skipping transport"
    local pcap
    pcap="$(python3 -c "
import json
with open('${existing}') as f:
    for l in f:
        l = l.strip()
        if not l: continue
        r = json.loads(l)
        if r.get('completed') and r.get('condition') == 'no_compression':
            print(r['capture_file'])
            break
" 2>/dev/null)"
    if [[ -z "${pcap}" || ! -f "${pcap}" ]]; then
      die "${label}: evidence row has no PCAP"
    fi
    echo "Validating: ${pcap}"
    validate_pcap "${pcap}" "${transport}" "${port}"
  else
    echo "${label}: no valid evidence — running exactly one no_compression request"
    local console_log
    console_log="${base_dir}/console.log"
    TRANSPORT="${transport}" \
      SAMPLES_OVERRIDE=1 \
      REPETITIONS_OVERRIDE=1 \
      RUNS_ROOT_OVERRIDE="${base_dir}" \
      PROFILE=pilot \
      CONDITION_OVERRIDE=no_compression \
      MAX_OUTPUT_TOKENS_OVERRIDE=4096 \
      OBSERVATION_SECONDS_OVERRIDE=900 \
      CAPTURE_STOP_ON_RESPONSE=true \
      CONNECTION_MODE=warm \
      CLIENT_NETNS="${CLIENT_NETNS:-llm-client}" \
      SECURE_PROXY_HOST="${SECURE_PROXY_HOST:-10.200.0.1}" \
      CAPTURE_INTERFACE_OVERRIDE="${CLIENT_VETH:-llmclient0}" \
      bash "${SCRIPT_DIR}/08_run_transport_profile.sh" \
      2>&1 | tee "${console_log}"
    echo "Transport exit status: ${PIPESTATUS[0]}" | tee -a "${console_log}"

    # Find the results file regardless of PROFILE suffix.
    local results_json
    results_json="$(ls -t "${base_dir}"/local_vllm_"${transport}"_*/results.jsonl 2>/dev/null | head -1)"
    if [[ -z "${results_json}" || ! -f "${results_json}" ]]; then
      echo "--- console log (last 40 lines) ---" >&2
      tail -40 "${console_log}" >&2
      die "${label} run produced no results.jsonl under ${base_dir}"
    fi
    echo "${label} results: ${results_json}"
    # Validate the single row
    python3 -c "
import json
with open('${results_json}') as f:
    rows = [json.loads(l) for l in f if l.strip()]
if len(rows) != 1:
    raise SystemExit('expected 1 ${label} result, got ' + str(len(rows)))
r = rows[0]
assert r.get('completed'), f'${label} not completed: {r.get(\"error\",\"?\")}'
assert r.get('finish_reason') == 'stop', f'${label} finish={r.get(\"finish_reason\")}'
assert r.get('transport') == '${transport}', f'${label} transport={r.get(\"transport\")}'
assert r.get('condition') == 'no_compression', f'${label} condition={r.get(\"condition\")}'
print(f'${label} OK: {r[\"request_id\"]} finish={r[\"finish_reason\"]} tokens={r.get(\"output_tokens\")} http_version={r.get(\"negotiated_http_version\")}')
"
    # Validate PCAP
    local pcaps
    pcaps="$(find "${base_dir}" -name '*.pcapng' -type f 2>/dev/null || true)"
    if [[ -z "${pcaps}" ]]; then
      die "No ${label} PCAP files found"
    fi
    for pcap in ${pcaps}; do
      echo "Validating: ${pcap}"
      validate_pcap "${pcap}" "${transport}" "${port}"
    done
  fi
}

# 5. TLS 1.3
echo ""
echo "============================================="
echo "  TLS 1.3 validation"
echo "============================================="
run_or_validate tls13 8443 tls

# 6. HTTP/3
echo ""
echo "============================================="
echo "  HTTP/3 validation"
echo "============================================="
run_or_validate http3 8444 http3

# 7. Report
echo ""
echo "============================================="
echo "  PROTOCOL VALIDATION COMPLETE"
echo "============================================="
echo "Results:   ${VALIDATION_ROOT}/"
echo ""
echo "Both protocol pilots passed validation."
echo "Full matrix is now gated on this success."
echo "Run: sudo --preserve-env=PATH EXPERIMENT_ENV_FILE=\$PWD/server.lab.env bash scripts/18_run_lab_matrix.sh"
