#!/usr/bin/env bash
# Temporary physical-firewall helper for client-to-server validation.
# Adds only transaction-tagged TLS/HTTP3 rules for explicit narrow client CIDRs.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ACTION="${1:-status}"
LISTENER_PROFILE="${LISTENER_PROFILE:-standard_https}"

case "${LISTENER_PROFILE}" in
  high_ports) TLS_PORT=8443; H3_PORT=8444 ;;
  standard_https) TLS_PORT=443; H3_PORT=443 ;;
  *) echo "LISTENER_PROFILE must be high_ports or standard_https" >&2; exit 2 ;;
esac

RUNS_ROOT_EFFECTIVE="${RUNS_ROOT:-runs}"
if [[ "${RUNS_ROOT_EFFECTIVE}" = /* ]]; then
  RUNS_ABS="${RUNS_ROOT_EFFECTIVE}"
else
  RUNS_ABS="${EXPERIMENT_ROOT}/${RUNS_ROOT_EFFECTIVE}"
fi
LOG_DIR="${FIREWALL_LOG_DIR:-${RUNS_ABS}/physical_validation/server/${LISTENER_PROFILE}}"
if [[ -L "${LOG_DIR}" ]]; then
  echo "ERROR: refusing symlinked firewall log directory: ${LOG_DIR}" >&2
  exit 2
fi
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/firewall.log"
STATE_FILE="${FIREWALL_STATE_FILE:-${LOG_DIR}/firewall.state}"
LOCK_FILE="${FIREWALL_LOCK_FILE:-${LOG_DIR}/firewall.lock}"

PHYS_IF="${PHYS_IF:-ens20f0}"
PHYS_IP="${PHYS_IP:-}"
if [[ ! "${PHYS_IF}" =~ ^[A-Za-z0-9_.-]+$ || ${#PHYS_IF} -gt 15 ]]; then
  echo "PHYS_IF must be a valid Linux interface name." >&2
  exit 2
fi
CLIENT_CIDRS="${CLIENT_CIDRS:-}"
RULE_TAG_PREFIX="commu-physical-client-${LISTENER_PROFILE}-${PHYS_IF}"
OWNER_TOKEN=""
BASELINE_FILE=""
TRANSACTION_MODE=""
DEFER_SIGNALS=0
PENDING_SIGNAL=0
ADDED_PROTOCOLS=()
ADDED_PORTS=()
ADDED_SOURCES=()
ADDED_TAGS=()

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: this script requires root (sudo)." >&2
    exit 2
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 2
  fi
}

refuse_symlinked_targets() {
  local target
  for target in "${LOG}" "${STATE_FILE}" "${LOCK_FILE}"; do
    if [[ -L "${target}" ]]; then
      echo "ERROR: refusing symlinked firewall artifact: ${target}" >&2
      exit 2
    fi
  done
}

acquire_mutation_lock() {
  require_command flock
  refuse_symlinked_targets
  exec 9>>"${LOCK_FILE}"
  if ! flock -n 9; then
    echo "ERROR: another firewall mutation owns ${LOCK_FILE}." >&2
    exit 2
  fi
}

log_msg() {
  if [[ -L "${LOG}" ]]; then
    echo "ERROR: refusing symlinked firewall log: ${LOG}" >&2
    return 1
  fi
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "${LOG}"
}

resolve_physical_destination() {
  require_command ip
  local -a assigned_ipv4=()
  mapfile -t assigned_ipv4 < <(
    ip -4 -o addr show dev "${PHYS_IF}" scope global |
      awk '{split($4, parts, "/"); print parts[1]}'
  )
  if ((${#assigned_ipv4[@]} == 0)); then
    echo "ERROR: no global IPv4 address is assigned to ${PHYS_IF}." >&2
    exit 2
  fi
  if [[ -z "${PHYS_IP}" ]]; then
    PHYS_IP="${assigned_ipv4[0]}"
  fi
  local assigned
  for assigned in "${assigned_ipv4[@]}"; do
    if [[ "${assigned}" == "${PHYS_IP}" ]]; then
      return 0
    fi
  done
  echo "ERROR: PHYS_IP ${PHYS_IP} is not assigned to ${PHYS_IF}." >&2
  exit 2
}

validate_ip_networks() {
  require_command python3
  local raw="$1" destination="$2"
  python3 - "${raw}" "${destination}" <<'PY'
import ipaddress
import sys

raw = sys.argv[1].replace(",", " ").split()
destination = ipaddress.ip_address(sys.argv[2])
if not raw:
    raise SystemExit("CLIENT_CIDRS must contain at least one CIDR")
seen = set()
for value in raw:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise SystemExit(f"invalid client CIDR {value!r}: {exc}") from exc
    minimum = 24 if network.version == 4 else 64
    if network.prefixlen < minimum:
        raise SystemExit(
            f"client CIDR {value!r} is too broad; require IPv4 /24+ or IPv6 /64+"
        )
    if network.is_unspecified or network.is_multicast:
        raise SystemExit(f"client CIDR {value!r} is not a usable source network")
    if network.version != destination.version:
        raise SystemExit(
            f"client CIDR {value!r} does not match destination {destination}"
        )
    canonical = str(network)
    if canonical not in seen:
        print(canonical)
        seen.add(canonical)
PY
}

validate_client_cidrs() {
  if [[ -z "${CLIENT_CIDRS//[[:space:],]/}" ]]; then
    echo "ERROR: apply requires explicit CLIENT_CIDRS (for example, 203.0.113.42/32)." >&2
    exit 2
  fi
  local normalized
  normalized="$(validate_ip_networks "${CLIENT_CIDRS}" "${PHYS_IP}")"
  mapfile -t CLIENT_SOURCES <<<"${normalized}"
}

state_value() {
  local key="$1"
  awk -F= -v key="${key}" \
    '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${STATE_FILE}"
}

write_state() {
  local status="$1" temporary="${STATE_FILE}.tmp.$$" index
  if [[ -L "${STATE_FILE}" || -e "${temporary}" || -L "${temporary}" ]]; then
    echo "ERROR: refusing unsafe firewall state target: ${STATE_FILE}" >&2
    return 1
  fi
  {
    printf 'owner=commu-physical-firewall-v1\n'
    printf 'status=%s\n' "${status}"
    printf 'owner_token=%s\n' "${OWNER_TOKEN}"
    printf 'listener_profile=%s\n' "${LISTENER_PROFILE}"
    printf 'physical_interface=%s\n' "${PHYS_IF}"
    printf 'physical_ip=%s\n' "${PHYS_IP}"
    printf 'baseline_file=%s\n' "${BASELINE_FILE}"
    for index in "${!ADDED_PROTOCOLS[@]}"; do
      [[ -n "${ADDED_PROTOCOLS[index]:-}" ]] || continue
      printf 'rule=%s|%s|%s|%s\n' \
        "${ADDED_PROTOCOLS[index]}" \
        "${ADDED_PORTS[index]}" \
        "${ADDED_SOURCES[index]}" \
        "${ADDED_TAGS[index]}"
    done
    printf 'updated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${temporary}"
  mv -T "${temporary}" "${STATE_FILE}"
}

load_state() {
  if [[ -L "${STATE_FILE}" ]]; then
    echo "ERROR: refusing symlinked firewall state: ${STATE_FILE}" >&2
    return 1
  fi
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "ERROR: no firewall ownership state exists at ${STATE_FILE}." >&2
    return 1
  fi
  if [[ "$(state_value owner)" != "commu-physical-firewall-v1" ||
    "$(state_value listener_profile)" != "${LISTENER_PROFILE}" ||
    "$(state_value physical_interface)" != "${PHYS_IF}" ]]; then
    echo "ERROR: firewall state ownership/profile/interface does not match." >&2
    return 1
  fi
  OWNER_TOKEN="$(state_value owner_token)"
  PHYS_IP="$(state_value physical_ip)"
  BASELINE_FILE="$(state_value baseline_file)"
  local baseline_parent baseline_name
  baseline_parent="$(dirname -- "${BASELINE_FILE}")"
  baseline_name="$(basename -- "${BASELINE_FILE}")"
  if [[ ! "${OWNER_TOKEN}" =~ ^[0-9a-f]{64}$ ||
    "${baseline_parent}" != "${LOG_DIR}" ||
    ! "${baseline_name}" =~ ^ufw_before\.[0-9]{8}T[0-9]{6}\.[0-9]+\.txt$ ||
    ! -f "${BASELINE_FILE}" || -L "${BASELINE_FILE}" ]]; then
    echo "ERROR: firewall state metadata is malformed or unsafe." >&2
    return 1
  fi
  if [[ "$(validate_ip_networks "${PHYS_IP}/32" "${PHYS_IP}")" != "${PHYS_IP}/32" ]]; then
    echo "ERROR: firewall state has an invalid physical destination." >&2
    return 1
  fi

  ADDED_PROTOCOLS=()
  ADDED_PORTS=()
  ADDED_SOURCES=()
  ADDED_TAGS=()
  local line protocol port source tag expected_port expected_tag source_canonical
  while IFS= read -r line; do
    [[ "${line}" == rule=* ]] || continue
    IFS='|' read -r protocol port source tag <<<"${line#rule=}"
    case "${protocol}" in
      tcp)
        expected_port="${TLS_PORT}"
        expected_tag="${RULE_TAG_PREFIX}-${OWNER_TOKEN:0:12}-tls"
        ;;
      udp)
        expected_port="${H3_PORT}"
        expected_tag="${RULE_TAG_PREFIX}-${OWNER_TOKEN:0:12}-h3"
        ;;
      *)
        echo "ERROR: invalid protocol in firewall state." >&2
        return 1
        ;;
    esac
    source_canonical="$(validate_ip_networks "${source}" "${PHYS_IP}")" || return 1
    if [[ "${source_canonical}" != "${source}" ||
      "${port}" != "${expected_port}" ||
      "${tag}" != "${expected_tag}" ]]; then
      echo "ERROR: invalid rule in firewall ownership state." >&2
      return 1
    fi
    ADDED_PROTOCOLS+=("${protocol}")
    ADDED_PORTS+=("${port}")
    ADDED_SOURCES+=("${source}")
    ADDED_TAGS+=("${tag}")
  done <"${STATE_FILE}"
}

profile_rule_bodies() {
  local source_file="$1"
  { grep -F "${RULE_TAG_PREFIX}-" "${source_file}" 2>/dev/null || true; } |
    sed -E 's/^\[[[:space:]]*[0-9]+\][[:space:]]*//' |
    sort
}

baseline_restored() {
  local current_file="${LOG_DIR}/.ufw_current.$$"
  if [[ -e "${current_file}" || -L "${current_file}" ]]; then
    echo "ERROR: refusing unsafe firewall verification target." >&2
    return 1
  fi
  if ! ufw status numbered >"${current_file}"; then
    rm -f "${current_file}"
    return 1
  fi
  local baseline_rules current_rules
  baseline_rules="$(profile_rule_bodies "${BASELINE_FILE}")"
  current_rules="$(profile_rule_bodies "${current_file}")"
  rm -f "${current_file}"
  [[ "${current_rules}" == "${baseline_rules}" ]]
}

delete_rule_exact() {
  local index="$1"
  ufw --force delete allow in on "${PHYS_IF}" \
    proto "${ADDED_PROTOCOLS[index]}" \
    from "${ADDED_SOURCES[index]}" to "${PHYS_IP}" \
    port "${ADDED_PORTS[index]}" \
    comment "${ADDED_TAGS[index]}" >>"${LOG}" 2>&1
}

handle_interrupt() {
  if [[ "${DEFER_SIGNALS}" -eq 1 ]]; then
    PENDING_SIGNAL=130
    return 0
  fi
  exit 130
}

handle_termination() {
  if [[ "${DEFER_SIGNALS}" -eq 1 ]]; then
    PENDING_SIGNAL=143
    return 0
  fi
  exit 143
}

finish_deferred_signals() {
  DEFER_SIGNALS=0
  if [[ "${PENDING_SIGNAL}" -ne 0 ]]; then
    exit "${PENDING_SIGNAL}"
  fi
}

delete_recorded_rules() {
  local failed=0 index
  for ((index = ${#ADDED_PROTOCOLS[@]} - 1; index >= 0; index--)); do
    [[ -n "${ADDED_PROTOCOLS[index]:-}" ]] || continue
    DEFER_SIGNALS=1
    if delete_rule_exact "${index}"; then
      local deleted_tag="${ADDED_TAGS[index]}"
      unset 'ADDED_PROTOCOLS[index]' 'ADDED_PORTS[index]' \
        'ADDED_SOURCES[index]' 'ADDED_TAGS[index]'
      write_state "${TRANSACTION_MODE}_in_progress"
      log_msg "deleted exact owned rule tag=${deleted_tag}" || true
      finish_deferred_signals
    else
      DEFER_SIGNALS=0
      echo "ERROR: failed exact deletion for owned rule ${ADDED_TAGS[index]}." >&2
      failed=1
    fi
  done
  return "${failed}"
}

transaction_exit() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT INT TERM
  DEFER_SIGNALS=0
  PENDING_SIGNAL=0
  if [[ "${TRANSACTION_MODE}" == "apply" ]]; then
    log_msg "apply interrupted/failed; rolling back exact transaction rules" || true
    delete_recorded_rules || cleanup_failed=1
    if [[ "${cleanup_failed}" -eq 0 ]] && baseline_restored; then
      rm -f "${STATE_FILE}"
      log_msg "rollback complete; pre-transaction profile rules preserved" || true
    else
      write_state cleanup_failed || true
      echo "ERROR: rollback incomplete; state remains at ${STATE_FILE}." >&2
      cleanup_failed=1
    fi
  elif [[ "${TRANSACTION_MODE}" == "cleanup" ]]; then
    write_state cleanup_failed || true
    cleanup_failed=1
  fi
  if [[ "${cleanup_failed}" -ne 0 && "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}

arm_transaction() {
  TRANSACTION_MODE="$1"
  trap transaction_exit EXIT
  trap handle_interrupt INT
  trap handle_termination TERM
}

disarm_transaction() {
  TRANSACTION_MODE=""
  trap - EXIT INT TERM
}

verify_rule_present() {
  local status="$1" protocol="$2" port="$3" source="$4" tag="$5"
  local source_status="${source}"
  if [[ "${source_status}" == */32 ]]; then
    source_status="${source_status%/32}"
  fi
  grep -F "${tag}" <<<"${status}" |
    grep -F "${port}/${protocol}" |
    grep -F "${source_status}" |
    grep -F "${PHYS_IP}" |
    grep -Fq "${PHYS_IF}"
}

status_fw() {
  require_command ufw
  refuse_symlinked_targets
  echo "Profile: ${LISTENER_PROFILE} (TLS ${TLS_PORT}, H3 ${H3_PORT})"
  echo "Destination interface: ${PHYS_IF}"
  echo "Configured destination address: ${PHYS_IP:-auto-discover on apply}"
  echo "Ownership state: ${STATE_FILE}"
  if [[ -f "${STATE_FILE}" && ! -L "${STATE_FILE}" ]]; then
    sed 's/^/  /' "${STATE_FILE}"
  else
    echo "  not recorded"
  fi
  echo "=== ufw status ==="
  ufw status verbose
  echo "Log: ${LOG}"
}

apply_fw() {
  require_root
  require_command ufw
  require_command sha256sum
  acquire_mutation_lock
  resolve_physical_destination
  validate_client_cidrs
  if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
    echo "ERROR: refusing to overwrite firewall ownership state ${STATE_FILE}." >&2
    exit 2
  fi

  OWNER_TOKEN="$(
    printf '%s\n' "$$:${RANDOM}:$(date -u +%s%N)" |
      sha256sum | awk '{print $1}'
  )"
  BASELINE_FILE="${LOG_DIR}/ufw_before.$(date -u +%Y%m%dT%H%M%S).$$.txt"
  if [[ -e "${BASELINE_FILE}" || -L "${BASELINE_FILE}" ]]; then
    echo "ERROR: refusing unsafe firewall baseline target ${BASELINE_FILE}." >&2
    exit 2
  fi
  if ! (set -o noclobber; ufw status numbered >"${BASELINE_FILE}"); then
    echo "ERROR: failed to capture pre-mutation UFW state." >&2
    exit 1
  fi
  if ! grep -Fq "Status: active" "${BASELINE_FILE}"; then
    echo "ERROR: UFW is not active; refusing to add inactive rules." >&2
    exit 1
  fi

  write_state applying
  arm_transaction apply
  log_msg "apply: profile=${LISTENER_PROFILE} interface=${PHYS_IF} destination=${PHYS_IP} TCP ${TLS_PORT} + UDP ${H3_PORT} from ${CLIENT_SOURCES[*]}"

  local source tag protocol port
  for source in "${CLIENT_SOURCES[@]}"; do
    for protocol in tcp udp; do
      if [[ "${protocol}" == "tcp" ]]; then
        port="${TLS_PORT}"
        tag="${RULE_TAG_PREFIX}-${OWNER_TOKEN:0:12}-tls"
      else
        port="${H3_PORT}"
        tag="${RULE_TAG_PREFIX}-${OWNER_TOKEN:0:12}-h3"
      fi
      DEFER_SIGNALS=1
      if ! ufw allow in on "${PHYS_IF}" proto "${protocol}" \
        from "${source}" to "${PHYS_IP}" port "${port}" \
        comment "${tag}" >>"${LOG}" 2>&1; then
        DEFER_SIGNALS=0
        echo "ERROR: failed to add ${protocol} firewall rule for ${source}." >&2
        exit 1
      fi
      ADDED_PROTOCOLS+=("${protocol}")
      ADDED_PORTS+=("${port}")
      ADDED_SOURCES+=("${source}")
      ADDED_TAGS+=("${tag}")
      write_state applying
      finish_deferred_signals
    done
  done

  local applied index
  if ! applied="$(ufw status numbered)"; then
    echo "ERROR: failed to read UFW state after applying rules." >&2
    exit 1
  fi
  if ! grep -Fq "Status: active" <<<"${applied}"; then
    echo "ERROR: UFW became inactive while applying rules." >&2
    exit 1
  fi
  for index in "${!ADDED_PROTOCOLS[@]}"; do
    if ! verify_rule_present \
      "${applied}" \
      "${ADDED_PROTOCOLS[index]}" \
      "${ADDED_PORTS[index]}" \
      "${ADDED_SOURCES[index]}" \
      "${ADDED_TAGS[index]}"; then
      echo "ERROR: failed to verify owned firewall rule ${ADDED_TAGS[index]}." >&2
      exit 1
    fi
  done
  write_state active
  disarm_transaction
  log_msg "applied and verified; ownership state=${STATE_FILE}"
  printf '\n%s\n' "${applied}"
}

cleanup_fw() {
  require_root
  require_command ufw
  acquire_mutation_lock
  load_state
  arm_transaction cleanup
  log_msg "cleanup: removing exact rules recorded in ${STATE_FILE}"
  if ! delete_recorded_rules; then
    echo "ERROR: exact firewall cleanup failed; ownership state is retained." >&2
    exit 1
  fi
  if ! baseline_restored; then
    echo "ERROR: current tagged rules differ from the pre-apply baseline." >&2
    exit 1
  fi
  rm -f "${STATE_FILE}"
  disarm_transaction
  log_msg "cleanup complete; pre-transaction profile rules preserved"
}

case "${ACTION}" in
  status) status_fw ;;
  apply) apply_fw ;;
  cleanup) cleanup_fw ;;
  *)
    echo "Usage: $0 {status|apply|cleanup}" >&2
    exit 2
    ;;
esac
