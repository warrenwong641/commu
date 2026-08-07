#!/usr/bin/env bash
# Discover the best capture interface for client-side packet capture.
# Safe: never guesses; reports candidates and exits with a clear message
# when auto-detection fails so the user can set CAPTURE_INTERFACE manually.
set -euo pipefail

echo "=== Capture interface discovery ==="
echo "OS: $(uname -s)"
echo ""

# Linux (WSL / native)
if [[ "$(uname -s)" == "Linux" ]]; then
  echo "Linux interfaces (WSL):"
  ip -br link show 2>/dev/null | grep -v 'lo\b' | while read -r ifname _ state _; do
    if [[ "${state}" == "UP" ]]; then
      mtu=$(cat "/sys/class/net/${ifname}/mtu" 2>/dev/null || echo "?")
      flags=""
      # vEthernet interfaces are virtual — frames may be TSO/GSO offloaded.
      # Prefer physical or VPN-tunnel adapters for on-wire packet sizing.
      if echo "${ifname}" | grep -qiE 'veth|vEthernet|docker|br-|tailscale'; then
        flags=" [VIRTUAL — may show offloaded frames; prefer physical/VPN adapter]"
      fi
      echo "  ${ifname}  state=UP  mtu=${mtu}${flags}"
    fi
  done
  echo ""
  # Best: physical/VPN egress, not virtual.  Fall back to any UP interface.
  CANDIDATE=""
  for pref in eth0 enp0s3 ens20f0 tun0 wg0; do
    if ip link show "${pref}" >/dev/null 2>&1; then
      st=$(ip -br link show "${pref}" 2>/dev/null | awk '{print $2}')
      if [[ "${st}" == "UP" ]]; then
        CANDIDATE="${pref}"
        break
      fi
    fi
  done
  if [[ -z "${CANDIDATE}" ]]; then
    CANDIDATE=$(ip -br link show 2>/dev/null | grep -v 'lo\b' | grep UP | grep -viE 'veth|vEthernet|docker|br-' | awk '{print $1}' | head -1)
  fi
  if [[ -z "${CANDIDATE}" ]]; then
    CANDIDATE=$(ip -br link show 2>/dev/null | grep -v 'lo\b' | grep UP | awk '{print $1}' | head -1)
  fi
  if [[ -n "${CANDIDATE}" ]]; then
    echo "Best candidate:  ${CANDIDATE}"
    echo "To use it: export CAPTURE_INTERFACE=${CANDIDATE}"
    echo ""
    echo "For on-wire MTU accuracy, capture on the physical/VPN egress adapter."
    echo "Capturing on vEthernet may show TSO/GSO offloaded super-frames."
    echo "Optional dual-capture: vEthernet for flow visibility + egress for sizing."
  else
    echo "No UP interfaces found besides lo."
    echo "Set CAPTURE_INTERFACE manually."
  fi

# Windows (WSL → powershell.exe)
elif [[ "$(uname -s)" == *"_NT"* || -n "${WINDIR:-}" ]]; then
  echo "Windows interfaces:"
  powershell.exe -Command "Get-NetAdapter | Where-Object Status -eq 'Up' | Format-Table Name, InterfaceDescription, LinkSpeed" 2>/dev/null || true
  echo ""
  echo "For Wireshark/tshark capture, use the Windows adapter name."
  echo "Wireshark can list interfaces:"
  echo '  & "C:\Program Files\Wireshark\tshark.exe" -D'
  echo ""
  echo "Set CAPTURE_INTERFACE to the adapter number or name, e.g.:"
  echo '  export CAPTURE_INTERFACE="Ethernet 2"  # or \Device\NPF_{...}'
fi

echo ""
echo "If no interface is auto-detected, set CAPTURE_INTERFACE manually"
echo "and rerun the client script."
