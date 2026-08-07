#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ACTION="${1:-status}"
CONDITION="${2:-baseline}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
CLIENT_IF="${CLIENT_VETH:-llmclient0}"
HOST_CIDR="${HOST_VETH_CIDR:-10.200.0.1/24}"
CLIENT_CIDR="${CLIENT_VETH_CIDR:-10.200.0.2/24}"
MTU="${NETWORK_MTU:-1500}"
RTT_MS="${NETWORK_RTT_MS:-40}"
UPLINK_MBIT="${NETWORK_UPLINK_MBIT:-20}"
DOWNLINK_MBIT="${NETWORK_DOWNLINK_MBIT:-50}"
QUEUE_PACKETS="${NETWORK_QUEUE_PACKETS:-1000}"
STATE_DIR="${NETWORK_STATE_DIR:-${EXPERIMENT_ROOT}/runs/network_state}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 2
  fi
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "This action requires root." >&2
    exit 2
  fi
}

for value_name in NETNS HOST_IF CLIENT_IF; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "${value_name} contains unsupported characters: ${value}" >&2
    exit 2
  fi
done
if ((${#HOST_IF} > 15 || ${#CLIENT_IF} > 15)); then
  echo "Linux interface names must be at most 15 characters." >&2
  exit 2
fi

STATE_FILE="${NETWORK_STATE_FILE:-${STATE_DIR}/${NETNS}.${HOST_IF}.state}"
LOCK_FILE="${STATE_FILE}.lock"
OWNER_TOKEN=""
OWNER_ALIAS=""
NAMESPACE_OWNED=0
VETH_OWNED=0
NAMESPACE_ID=""
HOST_IFINDEX=""
HOST_ALIAS_APPLIED=0
APPLY_IN_PROGRESS=0
DEFER_SIGNALS=0
PENDING_SIGNAL=0

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"
}

host_interface_exists() {
  ip link show dev "${HOST_IF}" >/dev/null 2>&1
}

client_interface_exists_in_namespace() {
  namespace_exists &&
    ip netns exec "${NETNS}" ip link show dev "${CLIENT_IF}" >/dev/null 2>&1
}

namespace_identity() {
  stat -Lc '%d:%i' "/var/run/netns/${NETNS}" 2>/dev/null || true
}

host_ifindex() {
  cat "/sys/class/net/${HOST_IF}/ifindex" 2>/dev/null || true
}

host_alias() {
  cat "/sys/class/net/${HOST_IF}/ifalias" 2>/dev/null || true
}

state_value() {
  local key="$1"
  [[ -f "${STATE_FILE}" ]] || return 0
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' \
    "${STATE_FILE}"
}

write_state() {
  local status="$1"
  local temporary="${STATE_FILE}.tmp.$$"
  if [[ -L "${STATE_FILE}" || -e "${temporary}" || -L "${temporary}" ]]; then
    echo "Refusing unsafe network state target: ${STATE_FILE}" >&2
    return 1
  fi
  umask 077
  mkdir -p "$(dirname -- "${STATE_FILE}")"
  {
    printf 'owner=commu-network-condition-v1\n'
    printf 'owner_token=%s\n' "${OWNER_TOKEN}"
    printf 'status=%s\n' "${status}"
    printf 'namespace=%s\n' "${NETNS}"
    printf 'host_veth=%s\n' "${HOST_IF}"
    printf 'client_veth=%s\n' "${CLIENT_IF}"
    printf 'namespace_owned=%s\n' "${NAMESPACE_OWNED}"
    printf 'namespace_id=%s\n' "${NAMESPACE_ID}"
    printf 'veth_owned=%s\n' "${VETH_OWNED}"
    printf 'host_ifindex=%s\n' "${HOST_IFINDEX}"
    printf 'host_alias=%s\n' "${OWNER_ALIAS}"
    printf 'host_alias_applied=%s\n' "${HOST_ALIAS_APPLIED}"
    printf 'condition=%s\n' "${CONDITION}"
    printf 'host_cidr=%s\n' "${HOST_CIDR}"
    printf 'client_cidr=%s\n' "${CLIENT_CIDR}"
    printf 'updated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${temporary}"
  mv "${temporary}" "${STATE_FILE}"
}

acquire_mutation_lock() {
  local state_parent
  state_parent="$(dirname -- "${STATE_FILE}")"
  if [[ -L "${state_parent}" ]]; then
    echo "Refusing symlinked network-state directory: ${state_parent}" >&2
    exit 2
  fi
  mkdir -p "${state_parent}"
  if [[ -L "${STATE_FILE}" || -L "${LOCK_FILE}" ]]; then
    echo "Refusing symlinked network state/lock target." >&2
    exit 2
  fi
  exec 9>>"${LOCK_FILE}"
  if ! flock -n 9; then
    echo "Another network-condition action owns ${LOCK_FILE}." >&2
    exit 2
  fi
}

load_owned_state() {
  if [[ -L "${STATE_FILE}" ]]; then
    echo "Refusing symlinked network ownership state: ${STATE_FILE}" >&2
    return 1
  fi
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "No project ownership state exists at ${STATE_FILE}; refusing name-only cleanup." >&2
    return 1
  fi
  if [[ "$(state_value owner)" != "commu-network-condition-v1" ]]; then
    echo "Unrecognized network ownership state: ${STATE_FILE}" >&2
    return 1
  fi
  if [[ "$(state_value namespace)" != "${NETNS}" ||
    "$(state_value host_veth)" != "${HOST_IF}" ||
    "$(state_value client_veth)" != "${CLIENT_IF}" ]]; then
    echo "Network ownership state does not match requested namespace/veth names." >&2
    return 1
  fi
  OWNER_TOKEN="$(state_value owner_token)"
  OWNER_ALIAS="$(state_value host_alias)"
  NAMESPACE_OWNED="$(state_value namespace_owned)"
  NAMESPACE_ID="$(state_value namespace_id)"
  VETH_OWNED="$(state_value veth_owned)"
  HOST_IFINDEX="$(state_value host_ifindex)"
  HOST_ALIAS_APPLIED="$(state_value host_alias_applied)"
  if [[ ! "${OWNER_TOKEN}" =~ ^[0-9a-f]{64}$ ||
    "${OWNER_ALIAS}" != "commu-network:${OWNER_TOKEN}" ||
    ! "${NAMESPACE_OWNED}" =~ ^[01]$ ||
    ! "${VETH_OWNED}" =~ ^[01]$ ||
    ! "${HOST_ALIAS_APPLIED}" =~ ^[01]$ ]]; then
    echo "Network ownership state is malformed; preserving it for inspection." >&2
    return 1
  fi
}

verify_owned_resources() {
  if host_interface_exists; then
    if [[ "${VETH_OWNED}" -ne 1 ||
      -z "${HOST_IFINDEX}" ||
      "$(host_ifindex)" != "${HOST_IFINDEX}" ||
      ("${HOST_ALIAS_APPLIED}" -eq 1 &&
        "$(host_alias)" != "${OWNER_ALIAS}") ]]; then
      echo "Refusing to remove ${HOST_IF}: interface identity/ownership does not match state." >&2
      return 1
    fi
  elif [[ "${VETH_OWNED}" -ne 1 && -n "${HOST_IFINDEX}" ]]; then
    echo "Malformed veth ownership metadata in ${STATE_FILE}." >&2
    return 1
  fi

  if namespace_exists; then
    if [[ "${NAMESPACE_OWNED}" -ne 1 ||
      -z "${NAMESPACE_ID}" ||
      "$(namespace_identity)" != "${NAMESPACE_ID}" ]]; then
      echo "Refusing to remove ${NETNS}: namespace identity/ownership does not match state." >&2
      return 1
    fi
  elif [[ "${NAMESPACE_OWNED}" -ne 1 && -n "${NAMESPACE_ID}" ]]; then
    echo "Malformed namespace ownership metadata in ${STATE_FILE}." >&2
    return 1
  fi
}

cleanup_owned_resources() {
  local failed=0
  verify_owned_resources || return 1

  if [[ "${NAMESPACE_OWNED}" -eq 1 ]] && namespace_exists; then
    if [[ "$(namespace_identity)" != "${NAMESPACE_ID}" ]]; then
      echo "Refusing late delete of ${NETNS}: namespace identity changed." >&2
      return 1
    fi
    if ! ip netns delete "${NETNS}"; then
      echo "Failed to delete owned namespace ${NETNS}." >&2
      failed=1
    fi
  fi
  if [[ "${VETH_OWNED}" -eq 1 ]] && host_interface_exists; then
    if [[ "$(host_ifindex)" != "${HOST_IFINDEX}" ||
      ("${HOST_ALIAS_APPLIED}" -eq 1 &&
        "$(host_alias)" != "${OWNER_ALIAS}") ]]; then
      echo "Refusing late delete of ${HOST_IF}: identity changed during cleanup." >&2
      failed=1
    elif ! ip link delete "${HOST_IF}"; then
      echo "Failed to delete owned host veth ${HOST_IF}." >&2
      failed=1
    fi
  fi
  if namespace_exists || host_interface_exists || client_interface_exists_in_namespace; then
    echo "Owned network resources remain after cleanup." >&2
    failed=1
  fi
  return "${failed}"
}

rollback_partial_apply() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT
  if [[ "${APPLY_IN_PROGRESS}" -eq 1 ]]; then
    cleanup_owned_resources || cleanup_failed=1
    if [[ "${cleanup_failed}" -eq 0 ]]; then
      rm -f "${STATE_FILE}"
    else
      write_state cleanup_failed || true
      echo "Partial apply cleanup failed; ownership state remains at ${STATE_FILE}." >&2
      [[ "${status}" -ne 0 ]] || status=1
    fi
  fi
  exit "${status}"
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

show_status() {
  echo "Ownership state: ${STATE_FILE}"
  if [[ -f "${STATE_FILE}" ]]; then
    sed 's/^/  /' "${STATE_FILE}"
  else
    echo "  not recorded"
  fi
  echo "Host interface: ${HOST_IF}"
  ip -details link show dev "${HOST_IF}" 2>/dev/null || true
  tc qdisc show dev "${HOST_IF}" 2>/dev/null || true
  ethtool -k "${HOST_IF}" 2>/dev/null |
    grep -E '^(tcp-segmentation-offload|generic-segmentation-offload|generic-receive-offload|large-receive-offload):' ||
    true
  echo "Client namespace/interface: ${NETNS}/${CLIENT_IF}"
  ip netns exec "${NETNS}" ip -details link show dev "${CLIENT_IF}" 2>/dev/null || true
  ip netns exec "${NETNS}" tc qdisc show dev "${CLIENT_IF}" 2>/dev/null || true
  ip netns exec "${NETNS}" ethtool -k "${CLIENT_IF}" 2>/dev/null |
    grep -E '^(tcp-segmentation-offload|generic-segmentation-offload|generic-receive-offload|large-receive-offload):' ||
    true
}

for command_name in ip tc ethtool stat flock sha256sum; do
  require_command "${command_name}"
done

case "${ACTION}" in
  apply)
    require_root
    case "${CONDITION}" in
      baseline | rtt | realistic) ;;
      *)
        echo "Condition must be baseline, rtt, or realistic; got ${CONDITION}." >&2
        exit 2
        ;;
    esac
    if ((MTU < 576 || MTU > 9000)); then
      echo "NETWORK_MTU must be between 576 and 9000; got ${MTU}." >&2
      exit 2
    fi
    if ((RTT_MS < 0 || RTT_MS % 2 != 0)); then
      echo "NETWORK_RTT_MS must be a non-negative even integer; got ${RTT_MS}." >&2
      exit 2
    fi
    if ((UPLINK_MBIT <= 0 || DOWNLINK_MBIT <= 0 || QUEUE_PACKETS <= 0)); then
      echo "Network rates and queue size must be positive." >&2
      exit 2
    fi

    acquire_mutation_lock
    if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
      echo "Refusing to overwrite network ownership state: ${STATE_FILE}" >&2
      echo "Inspect status and run reset with the same configuration." >&2
      exit 2
    fi
    if host_interface_exists || client_interface_exists_in_namespace || namespace_exists; then
      echo "Refusing to overwrite an existing ${HOST_IF}, ${CLIENT_IF}, or ${NETNS}." >&2
      exit 2
    fi

    OWNER_TOKEN="$(
      printf '%s\n' "$$:${RANDOM}:$(date -u +%s%N)" |
        sha256sum | awk '{print $1}'
    )"
    OWNER_ALIAS="commu-network:${OWNER_TOKEN}"
    write_state applying
    APPLY_IN_PROGRESS=1
    trap rollback_partial_apply EXIT
    trap handle_interrupt INT
    trap handle_termination TERM

    DEFER_SIGNALS=1
    ip netns add "${NETNS}"
    NAMESPACE_OWNED=1
    NAMESPACE_ID="$(namespace_identity)"
    [[ -n "${NAMESPACE_ID}" ]] || {
      echo "Could not record namespace identity for ${NETNS}." >&2
      exit 1
    }
    write_state applying
    finish_deferred_signals

    DEFER_SIGNALS=1
    ip link add "${HOST_IF}" type veth peer name "${CLIENT_IF}"
    VETH_OWNED=1
    HOST_IFINDEX="$(host_ifindex)"
    [[ -n "${HOST_IFINDEX}" ]] || {
      echo "Could not record host-veth identity for ${HOST_IF}." >&2
      exit 1
    }
    ip link set dev "${HOST_IF}" alias "${OWNER_ALIAS}"
    HOST_ALIAS_APPLIED=1
    write_state applying
    finish_deferred_signals

    ip link set "${CLIENT_IF}" netns "${NETNS}"
    ip address add "${HOST_CIDR}" dev "${HOST_IF}"
    ip netns exec "${NETNS}" ip address add "${CLIENT_CIDR}" dev "${CLIENT_IF}"
    ip link set dev "${HOST_IF}" mtu "${MTU}" up
    ip netns exec "${NETNS}" ip link set dev lo up
    ip netns exec "${NETNS}" ip link set dev "${CLIENT_IF}" mtu "${MTU}" up

    ethtool -K "${HOST_IF}" tso off gso off gro off lro off
    ip netns exec "${NETNS}" ethtool -K "${CLIENT_IF}" \
      tso off gso off gro off lro off

    if [[ "${CONDITION}" == "rtt" || "${CONDITION}" == "realistic" ]]; then
      half_rtt=$((RTT_MS / 2))
      if [[ "${CONDITION}" == "realistic" ]]; then
        # HOST_IF egress is server-to-client (downlink); CLIENT_IF egress is uplink.
        tc qdisc add dev "${HOST_IF}" root netem \
          delay "${half_rtt}ms" rate "${DOWNLINK_MBIT}mbit" limit "${QUEUE_PACKETS}"
        ip netns exec "${NETNS}" tc qdisc add dev "${CLIENT_IF}" root netem \
          delay "${half_rtt}ms" rate "${UPLINK_MBIT}mbit" limit "${QUEUE_PACKETS}"
      else
        tc qdisc add dev "${HOST_IF}" root netem delay "${half_rtt}ms"
        ip netns exec "${NETNS}" tc qdisc add dev "${CLIENT_IF}" \
          root netem delay "${half_rtt}ms"
      fi
    fi
    write_state active
    APPLY_IN_PROGRESS=0
    trap - EXIT
    trap - INT TERM
    show_status
    ;;
  reset)
    require_root
    acquire_mutation_lock
    load_owned_state
    cleanup_owned_resources
    rm -f "${STATE_FILE}"
    echo "Removed verified project-owned ${NETNS}/${HOST_IF}/${CLIENT_IF} resources."
    ;;
  status)
    show_status
    ;;
  *)
    echo "Usage: $0 {apply|reset|status} [baseline|rtt|realistic]" >&2
    exit 2
    ;;
esac
