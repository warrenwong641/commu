#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-$ROOT/config.env}"
# shellcheck disable=SC1090
source "$CONFIG"
: "${GATEWAY_HOST:?}" "${GATEWAY_PORT:?}" "${MODEL_NAME:?}" "${PROMPT:?}" "${MAX_TOKENS:?}"
: "${VLLM_API_KEY:?export the existing vLLM API key in memory before running}"
[[ "$GATEWAY_HOST" == "127.0.0.1" || "$GATEWAY_HOST" == "::1" ]] ||
  { echo "gateway must use a literal loopback IP address" >&2; exit 2; }
[[ "$GATEWAY_PORT" =~ ^[0-9]+$ && "$GATEWAY_PORT" -ge 1024 && "$GATEWAY_PORT" -le 65535 ]] ||
  { echo "choose an explicit high gateway port" >&2; exit 2; }

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
run_dir="$ROOT/runs/$run_id"
mkdir -p "$run_dir"
export EVIDENCE_DIR="$run_dir"
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

owned_pids=()
cleanup() {
  for pid in "${owned_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

env -u VLLM_API_KEY CUDA_VISIBLE_DEVICES= python3 "$ROOT/gateway.py" \
  --listen "$GATEWAY_HOST" --port "$GATEWAY_PORT" --upstream "$VLLM_URL" \
  >"$run_dir/gateway.log" 2>&1 &
owned_pids+=("$!")
printf '%s\n' "${owned_pids[@]}" > "$run_dir/owned_auxiliary_pids.txt"

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
CUDA_VISIBLE_DEVICES= python3 "$ROOT/client.py" \
  --url "http://$GATEWAY_HOST:$GATEWAY_PORT" --model "$MODEL_NAME" \
  --prompt "$PROMPT" --max-tokens "$MAX_TOKENS" --output "$run_dir/result.json"
env -u VLLM_API_KEY CUDA_VISIBLE_DEVICES= \
  python3 "$ROOT/analyze.py" "$run_dir/result.json" |
  tee "$run_dir/summary.json"
echo "run_dir=$run_dir"
