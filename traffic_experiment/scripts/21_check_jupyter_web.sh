#!/usr/bin/env bash
set -euo pipefail

PORT="${JUPYTER_PORT:-8888}"
CONFIG_HOME="${HOME}/.config/commu-jupyter"

systemctl --user --no-pager --full status commu-jupyter.service
echo
echo "Loopback listener:"
ss -ltnp | grep -E "127\\.0\\.0\\.1:${PORT}\\b" || {
  echo "Jupyter is not listening on loopback port ${PORT}." >&2
  exit 1
}
echo
echo "Local HTTP check:"
if [[ ! -s "${CONFIG_HOME}/token" ]]; then
  echo "Missing Jupyter token file: ${CONFIG_HOME}/token" >&2
  exit 1
fi
token="$(<"${CONFIG_HOME}/token")"
curl --fail --silent --show-error --output /dev/null --config - <<EOF
url = "http://127.0.0.1:${PORT}/api/status"
header = "Authorization: token ${token}"
EOF
unset token
echo "JUPYTER_LOCAL_OK"
if [[ -f "${CONFIG_HOME}/token" ]]; then
  echo "Token file exists with mode $(stat -c %a "${CONFIG_HOME}/token")."
fi
