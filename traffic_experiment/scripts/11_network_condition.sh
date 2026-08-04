#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
CONDITION="${2:-baseline}"
NETNS="${CLIENT_NETNS:-llm-client}"
HOST_IF="${HOST_VETH:-llmhost0}"
CLIENT_IF="${CLIENT_VETH:-llmclient0}"
HOST_CIDR="${HOST_VETH_CIDR:-10.200.0.1/24}"
CLIENT_CIDR="${CLIENT_VETH_CIDR:-10.200.0.2/24}"
MTU="${NETWORK_MTU:-1500}"
RTT_MS="${NETWORK_RTT_MS:-40}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 2
  fi
}

for command_name in ip tc ethtool; do
  require_command "${command_name}"
done

show_status() {
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

case "${ACTION}" in
  apply)
    if ip link show dev "${HOST_IF}" >/dev/null 2>&1 ||
      ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
      echo "Refusing to overwrite an existing ${HOST_IF} interface or ${NETNS} namespace." >&2
      echo "Inspect it with '$0 status' and explicitly run '$0 reset' if appropriate." >&2
      exit 2
    fi
    case "${CONDITION}" in
      baseline | rtt) ;;
      *)
        echo "Condition must be baseline or rtt; got ${CONDITION}." >&2
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

    ip netns add "${NETNS}"
    ip link add "${HOST_IF}" type veth peer name "${CLIENT_IF}"
    ip link set "${CLIENT_IF}" netns "${NETNS}"
    ip address add "${HOST_CIDR}" dev "${HOST_IF}"
    ip netns exec "${NETNS}" ip address add "${CLIENT_CIDR}" dev "${CLIENT_IF}"
    ip link set dev "${HOST_IF}" mtu "${MTU}" up
    ip netns exec "${NETNS}" ip link set dev lo up
    ip netns exec "${NETNS}" ip link set dev "${CLIENT_IF}" mtu "${MTU}" up

    ethtool -K "${HOST_IF}" tso off gso off gro off lro off
    ip netns exec "${NETNS}" ethtool -K "${CLIENT_IF}" \
      tso off gso off gro off lro off

    if [[ "${CONDITION}" == "rtt" ]]; then
      half_rtt=$((RTT_MS / 2))
      tc qdisc add dev "${HOST_IF}" root netem delay "${half_rtt}ms"
      ip netns exec "${NETNS}" tc qdisc add dev "${CLIENT_IF}" \
        root netem delay "${half_rtt}ms"
    fi
    show_status
    ;;
  reset)
    if ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
      ip netns delete "${NETNS}"
    fi
    if ip link show dev "${HOST_IF}" >/dev/null 2>&1; then
      ip link delete "${HOST_IF}"
    fi
    echo "Removed ${NETNS} and ${HOST_IF} when present."
    ;;
  status)
    show_status
    ;;
  *)
    echo "Usage: $0 {apply|reset|status} [baseline|rtt]" >&2
    exit 2
    ;;
esac
