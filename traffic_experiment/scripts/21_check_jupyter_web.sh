#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${JUPYTER_PORT:-8888}"

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] ||
  ((PORT < 1024 || PORT > 65535)); then
  echo "JUPYTER_PORT must be an unprivileged TCP port; got ${PORT}." >&2
  exit 2
fi
for command_name in curl ss; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command not found: ${command_name}" >&2
    exit 2
  fi
done

# The status action verifies the exact recorded service, unit, config,
# password-verifier metadata, and virtual-environment ownership before this
# checker contacts the listener.
JUPYTER_PORT="${PORT}" bash "${SCRIPT_DIR}/20_setup_jupyter_web.sh" status

echo
echo "Loopback listener:"
main_pid="$(
  systemctl --user show \
    --property=MainPID --value commu-jupyter.service
)"
if [[ ! "${main_pid}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Jupyter service has no valid MainPID." >&2
  exit 1
fi
listeners="$(ss -H -ltnp)"
if ! listener="$(
  grep -E "127\\.0\\.0\\.1:${PORT}[[:space:]]" <<<"${listeners}" |
    grep -F "pid=${main_pid},"
)"; then
  echo "Jupyter is not listening on loopback port ${PORT}." >&2
  exit 1
fi
printf '%s\n' "${listener}"

echo
echo "Unauthenticated local HTTP check:"
http_status="$(
  curl --silent --show-error --max-time 5 \
    --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:${PORT}/api/status"
)"
if [[ "${http_status}" != "403" ]]; then
  echo "Expected Jupyter /api/status to reject an unauthenticated request with 403; got ${http_status}." >&2
  exit 1
fi
echo "JUPYTER_LOCAL_AUTH_REQUIRED"
