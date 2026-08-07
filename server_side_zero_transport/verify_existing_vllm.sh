#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-$ROOT/config.env}"
[[ -f "$CONFIG" ]] || { echo "missing config: $CONFIG" >&2; exit 2; }
# shellcheck disable=SC1090
source "$CONFIG"

: "${ALLOWED_GPU_IDS:?set exactly two GPU IDs}"
: "${VLLM_PIDS:?set every existing vLLM PID}"
: "${VLLM_URL:?set existing loopback vLLM URL}"

python3 - "$VLLM_URL" <<'PY'
import ipaddress, sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
    raise SystemExit("VLLM_URL must be a plain http(s) URL")
try:
    ok = ipaddress.ip_address(u.hostname).is_loopback
except ValueError:
    ok = False
if not ok:
    raise SystemExit("VLLM_URL must use a literal loopback IP address")
PY

IFS=',' read -r -a allowed_raw <<< "$ALLOWED_GPU_IDS"
IFS=',' read -r -a service_pids <<< "$VLLM_PIDS"
[[ ${#allowed_raw[@]} -eq 2 ]] || { echo "exactly two ALLOWED_GPU_IDS required" >&2; exit 3; }
[[ ${#service_pids[@]} -gt 0 ]] || { echo "VLLM_PIDS is empty" >&2; exit 3; }

gpu_table="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits)"
proc_table="$(nvidia-smi --query-compute-apps=pid,gpu_uuid,process_name --format=csv,noheader,nounits)"

resolve_gpu() {
  local wanted="${1//[[:space:]]/}"
  awk -F',' -v w="$wanted" '{
    i=$1; u=$2; gsub(/[[:space:]]/,"",i); gsub(/^[[:space:]]+|[[:space:]]+$/,"",u)
    if (i==w || u==w) { print u; found++ }
  } END { if(found != 1) exit 1 }' <<< "$gpu_table"
}

allowed_a="$(resolve_gpu "${allowed_raw[0]}")" || { echo "first GPU ID is not unique/valid" >&2; exit 4; }
allowed_b="$(resolve_gpu "${allowed_raw[1]}")" || { echo "second GPU ID is not unique/valid" >&2; exit 4; }
[[ "$allowed_a" != "$allowed_b" ]] || { echo "GPU IDs resolve to the same device" >&2; exit 4; }

for pid in "${service_pids[@]}"; do
  pid="${pid//[[:space:]]/}"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] ||
    { echo "invalid or absent service PID: $pid" >&2; exit 5; }
done

service_names="$(
  for pid in "${service_pids[@]}"; do
    pid="${pid//[[:space:]]/}"
    tr -d '\n' < "/proc/$pid/comm"
    echo
  done
)"
grep -Eiq '(^vllm$|vllm::engine)' <<< "$service_names" ||
  { echo "listed PIDs do not contain recognizable vLLM processes" >&2; exit 5; }

used="$(
  for pid in "${service_pids[@]}"; do
    pid="${pid//[[:space:]]/}"
    awk -F',' -v p="$pid" '$1+0==p+0 {
      u=$2; gsub(/^[[:space:]]+|[[:space:]]+$/,"",u); print u
    }' <<< "$proc_table"
  done | sort -u
)"
[[ -n "$used" ]] || { echo "listed vLLM PIDs have no nvidia-smi compute evidence" >&2; exit 6; }
used_count="$(wc -l <<< "$used" | tr -d ' ')"
[[ "$used_count" -eq 2 ]] || { echo "service spans $used_count GPUs, required exactly 2" >&2; exit 6; }
grep -Fxq "$allowed_a" <<< "$used" && grep -Fxq "$allowed_b" <<< "$used" ||
  { echo "service GPU set differs from allowed GPU set" >&2; exit 6; }

evidence_dir="${EVIDENCE_DIR:-$ROOT/verification}"
mkdir -p "$evidence_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
evidence="$evidence_dir/$stamp.txt"
{
  echo "verified_utc=$(date -u +%FT%TZ)"
  echo "allowed_gpu_uuids=$allowed_a,$allowed_b"
  echo "vllm_pids=$VLLM_PIDS"
  echo "vllm_url=$VLLM_URL"
  echo
  echo "[nvidia-smi gpu inventory]"
  printf '%s\n' "$gpu_table"
  echo
  echo "[nvidia-smi compute processes]"
  printf '%s\n' "$proc_table"
  echo
  echo "[listed PID identities; command lines intentionally omitted]"
  for pid in "${service_pids[@]}"; do
    pid="${pid//[[:space:]]/}"
    printf '%s user=%s ppid=%s comm=%s\n' \
      "$pid" \
      "$(stat -c '%U' "/proc/$pid")" \
      "$(awk '/^PPid:/ {print $2}' "/proc/$pid/status")" \
      "$(tr -d '\n' < "/proc/$pid/comm")"
  done
} > "$evidence"
printf 'VERIFIED exactly two GPUs; evidence=%s\n' "$evidence"
