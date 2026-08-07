#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-$ROOT/config.env}"
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] ||
  { echo "missing, non-regular, or symlinked config: $CONFIG" >&2; exit 2; }
if grep -Eq \
  '^[[:space:]]*(export[[:space:]]+)?(LOCAL_VLLM_API_KEY|VLLM_API_KEY|OPENROUTER_API_KEY|GEMINI_API_KEY|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)[[:space:]]*=' \
  "$CONFIG"; then
  echo "credential assignment found in $CONFIG; export it only in the invoking process" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$CONFIG"
: "${GATEWAY_HOST:?}" "${GATEWAY_PORT:?}" "${MODEL_NAME:?}" "${PROMPT:?}" "${MAX_TOKENS:?}"
: "${VLLM_API_KEY:?export the existing vLLM API key in memory before running}"
[[ "$GATEWAY_HOST" == "127.0.0.1" || "$GATEWAY_HOST" == "::1" ]] ||
  { echo "gateway must use a literal loopback IP address" >&2; exit 2; }
[[ "$GATEWAY_PORT" =~ ^[0-9]+$ && "$GATEWAY_PORT" -ge 1024 && "$GATEWAY_PORT" -le 65535 ]] ||
  { echo "choose an explicit high gateway port" >&2; exit 2; }

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
run_dir="$(
  python3 - "$ROOT" "$run_id" <<'PY'
import os
import stat
import sys

root, run_id = sys.argv[1:]
if not run_id or "/" in run_id or run_id in {".", ".."}:
    raise SystemExit("invalid run identifier")

directory_flags = os.O_RDONLY | os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    directory_flags |= os.O_NOFOLLOW

root_fd = os.open(root, directory_flags)
try:
    try:
        os.mkdir("runs", 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    runs_stat = os.stat("runs", dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(runs_stat.st_mode):
        raise SystemExit("refusing non-directory or symlinked runs path")
    if runs_stat.st_uid != os.geteuid() or stat.S_IMODE(runs_stat.st_mode) & 0o077:
        raise SystemExit("runs directory must be owned by the invoking user and mode 0700")
    runs_fd = os.open("runs", directory_flags, dir_fd=root_fd)
    try:
        os.mkdir(run_id, 0o700, dir_fd=runs_fd)
        run_stat = os.stat(run_id, dir_fd=runs_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(run_stat.st_mode)
            or run_stat.st_uid != os.geteuid()
            or stat.S_IMODE(run_stat.st_mode) != 0o700
        ):
            raise SystemExit("new run directory failed private-directory validation")
        run_fd = os.open(run_id, directory_flags, dir_fd=runs_fd)
        try:
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                file_flags |= os.O_NOFOLLOW
            for name in (
                "verification.log",
                "gateway.log",
                "gateway.state",
                "result.json",
                "summary.json",
            ):
                fd = os.open(name, file_flags, 0o600, dir_fd=run_fd)
                os.close(fd)
        finally:
            os.close(run_fd)
    finally:
        os.close(runs_fd)
finally:
    os.close(root_fd)

print(os.path.join(root, "runs", run_id))
PY
)"

assert_private_artifact() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] ||
    { echo "refusing non-regular or symlinked run artifact: $path" >&2; return 1; }
  [[ "$(stat -c '%u' "$path")" == "$(id -u)" ]] ||
    { echo "refusing run artifact not owned by the invoking user: $path" >&2; return 1; }
  [[ "$(stat -c '%a' "$path")" == "600" ]] ||
    { echo "refusing run artifact without mode 0600: $path" >&2; return 1; }
}

export EVIDENCE_DIR="$run_dir"
assert_private_artifact "$run_dir/verification.log"
"$ROOT/verify_existing_vllm.sh" "$CONFIG" | tee "$run_dir/verification.log"

python3 - "$GATEWAY_HOST" "$GATEWAY_PORT" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET)
try:
    s.bind((sys.argv[1], int(sys.argv[2])))
except OSError as e:
    raise SystemExit(f"gateway port unavailable: {e}")
finally:
    s.close()
PY

gateway_pid=""
gateway_start_ticks=""
gateway_exe="$(readlink -f "$(command -v python3)")"
gateway_script="$(readlink -f "$ROOT/gateway.py")"
gateway_state="$run_dir/gateway.state"

process_start_ticks() {
  local pid="$1"
  awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true
}

process_state() {
  local pid="$1"
  awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || true
}

gateway_pid_matches() {
  local pid="$1" expected_ticks="$2"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -n "$expected_ticks" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ "$(process_state "$pid")" != "Z" ]] || return 1
  [[ "$(process_start_ticks "$pid")" == "$expected_ticks" ]] || return 1
  [[ "$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)" == "$gateway_exe" ]] ||
    return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline" || return 1
  local index saw_script=0 saw_listen=0 saw_port=0 saw_upstream=0
  for ((index = 0; index < ${#argv[@]}; index++)); do
    [[ "$(readlink -f "${argv[index]}" 2>/dev/null || true)" == "$gateway_script" ]] &&
      saw_script=1
    if [[ "${argv[index]}" == "--listen" &&
      "${argv[index + 1]:-}" == "$GATEWAY_HOST" ]]; then
      saw_listen=1
    fi
    if [[ "${argv[index]}" == "--port" &&
      "${argv[index + 1]:-}" == "$GATEWAY_PORT" ]]; then
      saw_port=1
    fi
    if [[ "${argv[index]}" == "--upstream" &&
      "${argv[index + 1]:-}" == "$VLLM_URL" ]]; then
      saw_upstream=1
    fi
  done
  [[ "$saw_script" -eq 1 && "$saw_listen" -eq 1 &&
    "$saw_port" -eq 1 && "$saw_upstream" -eq 1 ]]
}

wait_for_gateway_identity() {
  local attempts="$1" attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if gateway_pid_matches "$gateway_pid" "$gateway_start_ticks"; then
      return 0
    fi
    kill -0 "$gateway_pid" 2>/dev/null || return 1
    sleep 0.05
  done
  return 1
}

wait_for_gateway_exit() {
  local attempts="$1" attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if ! kill -0 "$gateway_pid" 2>/dev/null ||
      [[ "$(process_state "$gateway_pid")" == "Z" ]] ||
      [[ "$(process_start_ticks "$gateway_pid")" != "$gateway_start_ticks" ]]; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

gateway_listener_closed() {
  python3 - "$GATEWAY_HOST" "$GATEWAY_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET
with socket.socket(family) as sock:
    sock.settimeout(0.25)
    if sock.connect_ex((host, port)) == 0:
        raise SystemExit("gateway listener remains reachable")
PY
}

stop_owned_gateway() {
  [[ -n "$gateway_pid" ]] || return 0
  if ! kill -0 "$gateway_pid" 2>/dev/null ||
    [[ "$(process_state "$gateway_pid")" == "Z" ]]; then
    wait "$gateway_pid" 2>/dev/null || true
  elif gateway_pid_matches "$gateway_pid" "$gateway_start_ticks"; then
    kill -TERM "$gateway_pid" 2>/dev/null || true
    if ! wait_for_gateway_exit 50; then
      if gateway_pid_matches "$gateway_pid" "$gateway_start_ticks"; then
        kill -KILL "$gateway_pid" 2>/dev/null || true
      fi
      if ! wait_for_gateway_exit 20; then
        echo "verified gateway PID $gateway_pid did not exit" >&2
        return 1
      fi
    fi
    wait "$gateway_pid" 2>/dev/null || true
  else
    echo "refusing to signal PID $gateway_pid: gateway identity changed" >&2
    return 1
  fi
  if ! gateway_listener_closed; then
    echo "gateway process exited but listener closure was not verified" >&2
    return 1
  fi
  assert_private_artifact "$gateway_state"
  printf 'status=stopped\npid=%s\nstart_ticks=%s\nupstream=%s\n' \
    "$gateway_pid" "$gateway_start_ticks" "$VLLM_URL" >"$gateway_state"
  gateway_pid=""
  gateway_start_ticks=""
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT
  stop_owned_gateway || cleanup_failed=1
  if [[ "$cleanup_failed" -ne 0 && "$status" -eq 0 ]]; then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

assert_private_artifact "$run_dir/gateway.log"
env -u VLLM_API_KEY CUDA_VISIBLE_DEVICES= python3 "$ROOT/gateway.py" \
  --listen "$GATEWAY_HOST" --port "$GATEWAY_PORT" --upstream "$VLLM_URL" \
  >"$run_dir/gateway.log" 2>&1 &
gateway_pid=$!
gateway_start_ticks="$(process_start_ticks "$gateway_pid")"
if [[ -z "$gateway_start_ticks" ]] || ! wait_for_gateway_identity 40; then
  echo "gateway failed before exact process identity could be recorded" >&2
  exit 1
fi
assert_private_artifact "$gateway_state"
{
  echo "status=running"
  echo "pid=$gateway_pid"
  echo "start_ticks=$gateway_start_ticks"
  echo "executable=$gateway_exe"
  echo "script=$gateway_script"
  echo "listen_host=$GATEWAY_HOST"
  echo "listen_port=$GATEWAY_PORT"
  echo "upstream=$VLLM_URL"
} >"$gateway_state"

# Wait for the owned listener without making an HTTP or model request.
CUDA_VISIBLE_DEVICES= python3 - "$GATEWAY_HOST" "$GATEWAY_PORT" <<'PY'
import socket, sys, time
for _ in range(50):
    try:
        with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=.2):
            break
    except OSError:
        time.sleep(.1)
else:
    raise SystemExit("gateway did not become ready")
PY

# This is the first operation that may send a model request.
assert_private_artifact "$run_dir/result.json"
CUDA_VISIBLE_DEVICES= python3 "$ROOT/client.py" \
  --url "http://$GATEWAY_HOST:$GATEWAY_PORT" --model "$MODEL_NAME" \
  --prompt "$PROMPT" --max-tokens "$MAX_TOKENS" --output "$run_dir/result.json"
assert_private_artifact "$run_dir/summary.json"
env -u VLLM_API_KEY CUDA_VISIBLE_DEVICES= \
  python3 "$ROOT/analyze.py" "$run_dir/result.json" |
  tee "$run_dir/summary.json"
echo "run_dir=$run_dir"
