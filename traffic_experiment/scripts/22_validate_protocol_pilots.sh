#!/usr/bin/env bash
# Protocol validation pilots: exactly one QA request over TLS 1.3 and one
# over genuine HTTP/3/QUIC, with full PCAP validation, before the full matrix.
#
# Requires root (namespaces, veth, offload, tc).  Sources server.lab.env
# through lib.sh.  Append-only; never regenerates manifests or deletes partial
# runs.  Stops after both validations pass — does NOT launch the full matrix.
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
source "${SCRIPT_DIR}/protocol_admission.sh"

# --- helpers ---------------------------------------------------------------
die() { printf '%s\n' "$*" >&2; exit 1; }
require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "This script requires root; preserve PATH and LOCAL_VLLM_API_KEY with sudo"
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

# --- owned-resource lifecycle helpers --------------------------------------
VALIDATION_ROOT="$(absolute_from_experiment "${RUNS_ROOT}")/protocol_validation"
mkdir -p "${VALIDATION_ROOT}"
PROTOCOL_MARKER="$(protocol_validation_marker_path)"
LIFECYCLE_STATE="${VALIDATION_ROOT}/.lifecycle-$$.state"
NETWORK_OWNED=0
CADDY_OWNED=0
CADDY_PID=""
CADDY_START_TICKS=""
CADDY_CONFIG="${EXPERIMENT_ROOT}/configs/Caddyfile"
CADDY_EXE="$(command -v caddy || true)"

write_lifecycle_state() {
  {
    printf 'owner_pid=%s\n' "$$"
    printf 'network_owned=%s\n' "${NETWORK_OWNED}"
    printf 'namespace=%s\n' "${CLIENT_NETNS:-llm-client}"
    printf 'host_veth=%s\n' "${HOST_VETH:-llmhost0}"
    printf 'caddy_owned=%s\n' "${CADDY_OWNED}"
    printf 'caddy_pid=%s\n' "${CADDY_PID}"
    printf 'caddy_start_ticks=%s\n' "${CADDY_START_TICKS}"
    printf 'caddy_config=%s\n' "${CADDY_CONFIG}"
  } >"${LIFECYCLE_STATE}"
}

process_start_ticks() {
  local pid="$1"
  awk '{print $22}' "/proc/${pid}/stat" 2>/dev/null || true
}

caddy_pid_matches() {
  local pid="$1" expected_ticks="$2" expected_config="$3"
  [[ "${pid}" =~ ^[0-9]+$ && -n "${expected_ticks}" && -n "${expected_config}" ]] ||
    return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ "$(process_start_ticks "${pid}")" == "${expected_ticks}" ]] || return 1
  [[ -n "${CADDY_EXE}" &&
    "$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)" == "$(readlink -f "${CADDY_EXE}")" ]] ||
    return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/${pid}/cmdline" || return 1
  local index saw_run=0 saw_config=0
  for ((index = 0; index < ${#argv[@]}; index++)); do
    [[ "${argv[index]}" == "run" ]] && saw_run=1
    if [[ "${argv[index]}" == "--config" && "${argv[index + 1]:-}" == "${expected_config}" ]]; then
      saw_config=1
    fi
  done
  [[ "${saw_run}" -eq 1 && "${saw_config}" -eq 1 ]]
}

wait_for_owned_caddy_exit() {
  local pid="$1" expected_ticks="$2" expected_config="$3" attempts="$4"
  local attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    if ! caddy_pid_matches "${pid}" "${expected_ticks}" "${expected_config}"; then
      # The recorded process exited and the PID may have been reused. Never
      # wait on or signal a process whose identity no longer matches.
      return 0
    fi
    sleep 0.1
  done
  return 1
}

caddy_listeners_closed() {
  local tcp_listeners udp_listeners
  if ! command -v ss >/dev/null 2>&1; then
    echo "Cannot verify Caddy listener closure because ss is unavailable." >&2
    return 1
  fi
  if ! tcp_listeners="$(ss -H -ltn 2>/dev/null)" ||
    ! udp_listeners="$(ss -H -lun 2>/dev/null)"; then
    echo "Failed to inspect TCP/UDP listeners after stopping Caddy." >&2
    return 1
  fi
  if grep -Eq ':(8443|8543)[[:space:]]' <<<"${tcp_listeners}"; then
    echo "A TCP listener remains on a project Caddy port (8443 or 8543)." >&2
    return 1
  fi
  if grep -Eq ':(8444|8544)[[:space:]]' <<<"${udp_listeners}"; then
    echo "A UDP listener remains on a project Caddy port (8444 or 8544)." >&2
    return 1
  fi
}

stop_owned_caddy() {
  [[ "${CADDY_OWNED}" -eq 1 ]] || return 0
  if ! kill -0 "${CADDY_PID}" 2>/dev/null; then
    :
  elif caddy_pid_matches "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
    kill -TERM "${CADDY_PID}" 2>/dev/null || true
    if ! wait_for_owned_caddy_exit \
      "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}" 50; then
      # Recheck immediately before escalation so a PID reused after TERM is
      # never signalled as though it were still this invocation's Caddy.
      if caddy_pid_matches \
        "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
        kill -KILL "${CADDY_PID}" 2>/dev/null || true
        if ! wait_for_owned_caddy_exit \
          "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}" 20; then
          echo "Verified Caddy PID ${CADDY_PID} did not exit; preserving ownership state." >&2
          return 1
        fi
      fi
    fi
  else
    if ! kill -0 "${CADDY_PID}" 2>/dev/null; then
      CADDY_OWNED=0
      CADDY_PID=""
      CADDY_START_TICKS=""
      write_lifecycle_state
      return 0
    fi
    echo "Refusing to stop PID ${CADDY_PID}: Caddy ownership metadata no longer matches." >&2
    return 1
  fi
  if ! caddy_listeners_closed; then
    echo "Caddy process exited but listener closure could not be verified; preserving ownership state." >&2
    return 1
  fi
  CADDY_OWNED=0
  CADDY_PID=""
  CADDY_START_TICKS=""
  write_lifecycle_state
}

cleanup_resources() {
  local cleanup_failed=0
  stop_owned_caddy || cleanup_failed=1
  if [[ "${NETWORK_OWNED}" -eq 1 ]]; then
    if CLIENT_NETNS="${CLIENT_NETNS:-llm-client}" \
      HOST_VETH="${HOST_VETH:-llmhost0}" \
      NETWORK_MTU="${NETWORK_MTU:-1500}" \
      bash "${SCRIPT_DIR}/11_network_condition.sh" reset >/dev/null 2>&1; then
      NETWORK_OWNED=0
    else
      echo "Failed to remove owned namespace/veth; preserving lifecycle state." >&2
      cleanup_failed=1
    fi
  fi
  if [[ "${cleanup_failed}" -eq 0 ]]; then
    if ! rm -f "${LIFECYCLE_STATE}"; then
      cleanup_failed=1
    fi
  fi
  if [[ "${cleanup_failed}" -ne 0 ]]; then
    write_lifecycle_state
    echo "Cleanup incomplete; ownership metadata remains at ${LIFECYCLE_STATE}." >&2
    return 1
  fi
  return 0
}

cleanup() {
  local status=$?
  trap - EXIT
  if ! cleanup_resources; then
    [[ "${status}" -ne 0 ]] || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
write_lifecycle_state

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
  if [[ "${max_ip:-0}" -le 0 ]]; then
    die "FAIL: no IP packet lengths decoded from ${pcap}"
  fi
  if [[ "${max_ip}" -gt "${NETWORK_MTU:-1500}" ]]; then
    die "FAIL: max IP packet ${max_ip} > configured MTU ${NETWORK_MTU:-1500}"
  fi

  # Unrelated traffic check: only port ${port} traffic should exist
  local other_count
  other_count="$(tshark -r "${pcap}" -Y "tcp || udp" -T fields -e frame.number 2>/dev/null | wc -l)"
  local port_count
  port_count="$(tshark -r "${pcap}" -Y "(tcp.port == ${port}) || (udp.port == ${port})" -T fields -e frame.number 2>/dev/null | wc -l)"
  if [[ "${other_count}" -gt "${port_count}" ]]; then
    die "FAIL: PCAP contains unrelated traffic (${other_count} total vs ${port_count} on port ${port})"
  fi
  echo "  capture_filter_clean (OK)"
}

# --- main ------------------------------------------------------------------
require_root
require_value LOCAL_VLLM_API_KEY

# 1. Preflight
echo "=== Preflight ==="
EXPERIMENT_ENV_FILE="${ENV_FILE}" bash "${SCRIPT_DIR}/17_lab_preflight.sh" || die "preflight failed"

# 2. Apply the exact full-matrix baseline topology.
echo "=== Network: baseline apply ==="
if ip link show dev "${HOST_VETH:-llmhost0}" >/dev/null 2>&1 ||
  ip netns list | awk '{print $1}' | grep -Fxq "${CLIENT_NETNS:-llm-client}"; then
  die "Refusing to replace existing ${HOST_VETH:-llmhost0} or ${CLIENT_NETNS:-llm-client}; inspect and remove it explicitly"
fi
CLIENT_NETNS="${CLIENT_NETNS:-llm-client}" \
  HOST_VETH="${HOST_VETH:-llmhost0}" \
  NETWORK_MTU="${NETWORK_MTU:-1500}" \
  bash "${SCRIPT_DIR}/11_network_condition.sh" apply baseline
# The transactional helper rolls back partial failures. Claim outer cleanup
# only after this invocation successfully created the controlled network.
NETWORK_OWNED=1
write_lifecycle_state

# 3. Calibrate the veth link (outside capture window)
echo "=== Link calibration (baseline veth) ==="
LINK_CALIBRATION_LABEL="protocol_validation" \
  bash "${SCRIPT_DIR}/15_calibrate_link.sh"

# 4. Start the exact shared Caddy configuration on the host veth IP
#    (10.200.0.1). Its global auto_https policy disables redirect listeners,
#    so this project never attempts to claim the unrelated service's port 80.
echo "=== Caddy start (auto_https disabled, on ${SECURE_PROXY_HOST:-10.200.0.1}) ==="
write_lifecycle_state
# Use host veth IP from server.lab.env so client in namespace can connect.
export VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT SECURE_PROXY_HOST
CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
mkdir -p "${CADDY_RUN_DIR}"
export XDG_DATA_HOME="${CADDY_RUN_DIR}/data"
export XDG_CONFIG_HOME="${CADDY_RUN_DIR}/config"
caddy validate --config "${CADDY_CONFIG}" --adapter caddyfile
caddy run --config "${CADDY_CONFIG}" --adapter caddyfile \
  >"${VALIDATION_ROOT}/caddy-$$.log" 2>&1 &
CADDY_PID=$!
CADDY_START_TICKS="$(process_start_ticks "${CADDY_PID}")"
CADDY_OWNED=1
write_lifecycle_state
sleep 1
if ! caddy_pid_matches "${CADDY_PID}" "${CADDY_START_TICKS}" "${CADDY_CONFIG}"; then
  die "Project Caddy failed to start; see ${VALIDATION_ROOT}/caddy-$$.log"
fi
echo "Caddy started with auto_https disabled."

# Only evidence
# with an immutable marker bound to the current row, PCAP, manifest, model,
# protocol stack, capture configuration, and generation definition is reusable.
LAST_PILOT_EVIDENCE_MARKER=""
run_or_validate_strict() {
  local transport="$1" port="$2" label="$3"
  local capture_filter base_prefix existing_marker pcap
  capture_filter="$(protocol_pilot_capture_filter "${transport}")"
  base_prefix="${VALIDATION_ROOT}/${label}"
  existing_marker=""

  local -a candidate_dirs=() run_dirs=()
  shopt -s nullglob
  candidate_dirs=("${base_prefix}" "${base_prefix}"_retry*)
  for candidate_dir in "${candidate_dirs[@]}"; do
    run_dirs=("${candidate_dir}"/local_vllm_"${transport}"_*)
    for run_dir in "${run_dirs[@]}"; do
      local results_candidate marker_candidate
      results_candidate="${run_dir}/results.jsonl"
      marker_candidate="${run_dir}/PILOT_EVIDENCE_OK.json"
      if [[ -e "${marker_candidate}" || -L "${marker_candidate}" ]]; then
        if ! pcap="$(
          protocol_pilot_evidence verify \
            "${marker_candidate}" "${transport}" "${results_candidate}"
        )"; then
          die "${label}: immutable pilot evidence is invalid at ${marker_candidate}"
        fi
        existing_marker="${marker_candidate}"
        break 2
      fi
    done
  done
  shopt -u nullglob

  if [[ -n "${existing_marker}" ]]; then
    echo "${label}: validated immutable evidence found; skipping transport"
    echo "Validating: ${pcap}"
    validate_pcap "${pcap}" "${transport}" "${port}"
    LAST_PILOT_EVIDENCE_MARKER="${existing_marker}"
    return 0
  fi

  echo "${label}: no immutable evidence; running exactly one no_compression request"
  local base_dir console_log
  base_dir="$(_safe_dir "${base_prefix}")"
  mkdir -p "${base_dir}"
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
    CAPTURE_FILTER_OVERRIDE="${capture_filter}" \
    bash "${SCRIPT_DIR}/08_run_transport_profile.sh" \
    2>&1 | tee "${console_log}"

  local -a results_candidates=()
  local results_json evidence_marker
  shopt -s nullglob
  results_candidates=(
    "${base_dir}"/local_vllm_"${transport}"_*/results.jsonl
  )
  shopt -u nullglob
  if [[ "${#results_candidates[@]}" -ne 1 ]]; then
    echo "--- console log (last 40 lines) ---" >&2
    tail -40 "${console_log}" >&2
    die "${label} run produced ${#results_candidates[@]} results files; expected exactly one"
  fi
  results_json="${results_candidates[0]}"
  evidence_marker="$(dirname -- "${results_json}")/PILOT_EVIDENCE_OK.json"
  pcap="$(
    protocol_pilot_evidence check \
      "${evidence_marker}" "${transport}" "${results_json}"
  )"
  echo "Validating: ${pcap}"
  validate_pcap "${pcap}" "${transport}" "${port}"
  protocol_pilot_evidence mark \
    "${evidence_marker}" "${transport}" "${results_json}" >/dev/null
  protocol_pilot_evidence verify \
    "${evidence_marker}" "${transport}" "${results_json}" >/dev/null
  LAST_PILOT_EVIDENCE_MARKER="${evidence_marker}"
}

# 5. TLS 1.3
echo ""
echo "============================================="
echo "  TLS 1.3 validation"
echo "============================================="
run_or_validate_strict tls13 8443 tls
TLS_PILOT_EVIDENCE_MARKER="${LAST_PILOT_EVIDENCE_MARKER}"

# 6. HTTP/3
echo ""
echo "============================================="
echo "  HTTP/3 validation"
echo "============================================="
run_or_validate_strict http3 8444 http3
HTTP3_PILOT_EVIDENCE_MARKER="${LAST_PILOT_EVIDENCE_MARKER}"

# 7. Report
cleanup_resources
trap - EXIT
write_protocol_success_marker \
  "${PROTOCOL_MARKER}" \
  "${TLS_PILOT_EVIDENCE_MARKER}" \
  "${HTTP3_PILOT_EVIDENCE_MARKER}"
echo ""
echo "============================================="
echo "  PROTOCOL VALIDATION COMPLETE"
echo "============================================="
echo "Results:   ${VALIDATION_ROOT}/"
echo ""
echo "Both protocol pilots passed validation."
echo "Admission marker: ${PROTOCOL_MARKER}"
echo "Full matrix is now gated on this immutable success marker."
echo "Run: sudo --preserve-env=PATH,LOCAL_VLLM_API_KEY EXPERIMENT_ENV_FILE=\$PWD/server.lab.env bash scripts/18_run_lab_matrix.sh"
