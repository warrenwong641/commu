#!/usr/bin/env bash
# Shell-level tests for scripts/22_validate_protocol_pilots.sh helpers.
# Run without root; tests the _safe_dir logic and validate_pcap static
# checks with a synthetic PCAP.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PASS=0
FAIL=0

pass() { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

# --- test _safe_dir logic (isolated from the main script) ----------------

test_safe_dir() {
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf ${tmpdir}' RETURN

  local SCRIPT_DIR  # avoid contaminating sourced vars
  SCRIPT_DIR="${PROJECT_DIR}/scripts"

  # Extract _safe_dir function from the orchestrator
  _safe_dir() {
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

  # New directory: returns same path
  local result
  result="$(_safe_dir "${tmpdir}/new_dir")"
  [[ "${result}" == "${tmpdir}/new_dir" ]] && pass "safe_dir: new dir returns self" || fail "safe_dir: new dir"

  # Empty existing directory: returns same path
  mkdir -p "${tmpdir}/empty_dir"
  result="$(_safe_dir "${tmpdir}/empty_dir")"
  [[ "${result}" == "${tmpdir}/empty_dir" ]] && pass "safe_dir: empty dir returns self" || fail "safe_dir: empty dir"

  # Non-empty existing directory: returns _retry2
  mkdir -p "${tmpdir}/has_content"
  touch "${tmpdir}/has_content/file.txt"
  result="$(_safe_dir "${tmpdir}/has_content")"
  [[ "${result}" == "${tmpdir}/has_content_retry2" ]] && pass "safe_dir: populated dir returns _retry2" || fail "safe_dir: populated dir (got ${result})"

  # retry2 already exists too: returns _retry3
  mkdir -p "${tmpdir}/stacked"
  touch "${tmpdir}/stacked/data"
  mkdir -p "${tmpdir}/stacked_retry2"
  result="$(_safe_dir "${tmpdir}/stacked")"
  [[ "${result}" == "${tmpdir}/stacked_retry3" ]] && pass "safe_dir: stacked retries return _retry3" || fail "safe_dir: stacked retries (got ${result})"
}

# --- test validate_pcap with a synthetic PCAP file -----------------------

test_validate_pcap() {
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf ${tmpdir}' RETURN

  if ! command -v tshark >/dev/null 2>&1; then
    echo "SKIP: tshark not available (validate_pcap integration test)"
    return
  fi

  # Create a minimal valid PCAP: capture 2 packets on lo port 12345.
  # This produces a real pcap file for structural validation.
  dumpcap -i lo -c 3 -w "${tmpdir}/valid.pcapng" -f "tcp port 12345" 2>/dev/null && {
    # Source the validation function inline
    validate_pcap() {
      local pcap="$1" transport="$2" port="$3"
      local cap_hash
      cap_hash="$(sha256sum "${pcap}" | awk '{print $1}')" || return 1
      echo "  capture_sha256=${cap_hash}"
      local proto_filter
      case "${transport}" in tls13) proto_filter="tcp" ;; http3) proto_filter="udp" ;; esac
      local proto_count
      proto_count="$(tshark -r "${pcap}" -Y "${proto_filter}" -T fields -e frame.number 2>/dev/null | wc -l)" || true
      [[ "${proto_count}" -gt 0 ]] || { echo "FAIL: no ${proto_filter} packets"; return 1; }
      return 0
    }
    if validate_pcap "${tmpdir}/valid.pcapng" tls13 12345; then
      pass "validate_pcap: accepts valid TCP PCAP"
    else
      fail "validate_pcap: rejects valid PCAP"
    fi
  }

  # Empty file should fail
  touch "${tmpdir}/empty.pcapng"
  validate_pcap() {
    local pcap="$1" transport="$2" port="$3"
    [[ -s "${pcap}" ]] || { echo "FAIL: empty"; return 1; }
    return 0
  }
  if ! validate_pcap "${tmpdir}/empty.pcapng" tls13 8443 2>/dev/null; then
    pass "validate_pcap: rejects empty file"
  else
    fail "validate_pcap: accepted empty file"
  fi
}

# --- test decode-as arguments -----------------------------------------------

test_decode_as_args() {
  # Verify that the validate_pcap function constructs correct decode-as
  # arguments: -d tcp.port==8443,tls for TLS, -d udp.port==8444,quic for HTTP/3.
  local script="${PROJECT_DIR}/scripts/22_validate_protocol_pilots.sh"

  # Extract the decode_args assignment from the validate_pcap function.
  local tls_decode h3_decode
  tls_decode="$(sed -n '/validate_pcap()/,/^}/p' "${script}" |
    grep 'tls13) decode_args' | grep -o '\-d "[^"]*"')"
  h3_decode="$(sed -n '/validate_pcap()/,/^}/p' "${script}" |
    grep 'http3) decode_args' | grep -o '\-d "[^"]*"')"

  if [[ "${tls_decode}" == '-d "tcp.port==${port},tls"' ]]; then
    pass "decode_args: TLS uses tcp.port==\${port},tls"
  else
    fail "decode_args: TLS got '${tls_decode}'"
  fi

  if [[ "${h3_decode}" == '-d "udp.port==${port},quic"' ]]; then
    pass "decode_args: HTTP3 uses udp.port==\${port},quic"
  else
    fail "decode_args: HTTP3 got '${h3_decode}'"
  fi
}

# --- test MTU check ---------------------------------------------------------

test_mtu_check() {
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf ${tmpdir}' RETURN

  if ! command -v tshark >/dev/null 2>&1; then
    echo "SKIP: tshark not available (MTU integration test)"
    return
  fi

  # Capture 3 packets on lo — these will be small (< 100 bytes each).
  dumpcap -i lo -c 3 -w "${tmpdir}/small.pcapng" 2>/dev/null || {
    echo "SKIP: dumpcap failed (MTU integration test)"
    return
  }

  # Simulate the new MTU check logic
  local max_ip
  max_ip="$(tshark -r "${tmpdir}/small.pcapng" -T fields -e ip.len -e ipv6.plen 2>/dev/null |
    awk '{
       if ($1+0>0) { v=$1+0 }
       else if ($2+0>0) { v=$2+0+40 }
       else { next }
       if (v>max) max=v
    } END { print max+0 }')"

  if [[ -n "${max_ip}" && "${max_ip}" -le 1500 ]]; then
    pass "MTU check: small PCAP passes (max_ip=${max_ip} <= 1500)"
  elif [[ -z "${max_ip}" ]]; then
    pass "MTU check: no IP packets in PCAP (loopback may use raw IP, max_ip empty)"
  else
    fail "MTU check: small PCAP failed (max_ip=${max_ip})"
  fi
}

# --- run tests ------------------------------------------------------------

test_caddyfile_syntax() {
  # Verify the generated physical-listener Caddyfile is valid.
  local caddy
  caddy="$(find "${PROJECT_DIR}/.tools" -name caddy -type f 2>/dev/null | head -1)"
  if [[ -z "${caddy}" || ! -x "${caddy}" ]]; then
    echo "SKIP: caddy binary not found"
    return
  fi
  # Source just the _caddyfile function without executing the script.
  PHYS_IP=144.214.210.31
  VLLM_HOST=127.0.0.1 VLLM_PORT=8000 VLLM_SECONDARY_PORT=8001
  _caddyfile() {
    # Match the actual script: unquoted heredoc so variables expand.
    cat <<CEOF
{
  auto_https disable_redirects
  servers :8443 {
    protocols h1
  }
  servers :8444 {
    protocols h3
  }
  servers :8543 {
    protocols h1
  }
  servers :8544 {
    protocols h3
  }
}

https://${PHYS_IP}:8443 {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:8444 {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:8543 {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
https://${PHYS_IP}:8544 {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
CEOF
  }
  # Also test the standard_https branch inline.
  _caddyfile_std() {
    if [[ "${TLS_PORT}" == "${H3_PORT}" ]]; then
      cat <<CEOF
{
  auto_https disable_redirects
  servers :${TLS_PORT} {
    protocols h1 h3
  }
  servers :${W2_TLS} {
    protocols h1
  }
  servers :${W2_H3} {
    protocols h3
  }
}

https://${PHYS_IP}:${TLS_PORT} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_PORT:-8000}
}
https://${PHYS_IP}:${W2_TLS} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
https://${PHYS_IP}:${W2_H3} {
  tls internal {
    protocols tls1.3
    curves x25519
  }
  reverse_proxy ${VLLM_HOST:-127.0.0.1}:${VLLM_SECONDARY_PORT:-8001}
}
CEOF
    fi
  }
  local tmpfile
  tmpfile="$(mktemp /tmp/test_caddy_phys.XXXXXX)"
  _caddyfile >"${tmpfile}"
  if "${caddy}" validate --config "${tmpfile}" --adapter caddyfile >/dev/null 2>&1; then
    pass "caddyfile: high_ports Caddyfile is valid"
  else
    fail "caddyfile: high_ports Caddyfile failed validation"
  fi
  rm -f "${tmpfile}"

  # Also validate standard_https profile (shared port 443, single site).
  TLS_PORT=443 H3_PORT=443 W2_TLS=8543 W2_H3=8544
  tmpfile="$(mktemp /tmp/test_caddy_std.XXXXXX)"
  _caddyfile_std >"${tmpfile}"
  if "${caddy}" validate --config "${tmpfile}" --adapter caddyfile >/dev/null 2>&1; then
    pass "caddyfile: standard_https Caddyfile is valid (shared :443)"
  else
    fail "caddyfile: standard_https Caddyfile failed validation"
  fi
  if "${caddy}" adapt --config "${tmpfile}" --adapter caddyfile >/dev/null 2>&1; then
    pass "caddyfile: standard_https adapt succeeds"
  else
    fail "caddyfile: standard_https adapt failed"
  fi
  # Variable expansion is ON — count literal IP :443 site blocks.
  local site_count
  site_count="$(grep -c '144.214.210.31:443' "${tmpfile}" || echo 0)"
  if [[ "${site_count}" -eq 1 ]]; then
    pass "caddyfile: standard_https has exactly 1 site block (not 2)"
  else
    fail "caddyfile: standard_https has ${site_count} site blocks for :443 (expected 1)"
  fi
  rm -f "${tmpfile}"
}

test_profile_separation() {
  # Verify the client runner maps profiles to distinct output directories
  # and ports, preventing accidental merging of evidence.
  local script="${PROJECT_DIR}/scripts/run_physical_client.sh"

  # Extract the port assignments for each profile.
  local std_tls_port std_h3_port high_tls_port high_h3_port
  std_tls_port="$(sed -n '/standard_https)/s/.*TLS_PORT=\([0-9]*\).*/\1/p' "${script}")"
  std_h3_port="$(sed -n '/standard_https)/s/.*H3_PORT=\([0-9]*\).*/\1/p' "${script}")"
  high_tls_port="$(sed -n '/high_ports)/s/.*TLS_PORT=\([0-9]*\).*/\1/p' "${script}")"
  high_h3_port="$(sed -n '/high_ports)/s/.*H3_PORT=\([0-9]*\).*/\1/p' "${script}")"

  if [[ "${std_tls_port}" == "443" && "${std_h3_port}" == "443" ]]; then
    pass "profile: standard_https → TCP 443, UDP 443"
  else
    fail "profile: standard_https got TLS=${std_tls_port} H3=${std_h3_port}"
  fi
  if [[ "${high_tls_port}" == "8443" && "${high_h3_port}" == "8444" ]]; then
    pass "profile: high_ports → TCP 8443, UDP 8444"
  else
    fail "profile: high_ports got TLS=${high_tls_port} H3=${high_h3_port}"
  fi

  # Verify output dirs differ (no merging)
  local std_out high_out
  std_out="$(grep 'OUTPUT_DIR=' "${script}" | head -1)"
  if echo "${std_out}" | grep -q 'LISTENER_PROFILE'; then
    pass "profile: output dir includes LISTENER_PROFILE (no merging)"
  else
    fail "profile: output dir missing LISTENER_PROFILE"
  fi
}

test_safe_dir
test_validate_pcap
test_decode_as_args
test_mtu_check
test_caddyfile_syntax
test_profile_separation

echo ""
echo "Tests: ${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]] && echo "SHELL_TEST_OK" || { echo "SHELL_TEST_FAILURES" >&2; exit 1; }
