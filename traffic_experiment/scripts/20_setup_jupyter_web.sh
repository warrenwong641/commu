#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
WEB_ROOT="${EXPERIMENT_ROOT}/web_control"

for command_name in python3 openssl systemctl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command not found: ${command_name}" >&2
    exit 2
  fi
done
if [[ "${EUID}" -eq 0 ]]; then
  echo "Do not run JupyterLab as root. Run this as the dedicated lab user." >&2
  exit 2
fi

JUPYTER_VENV="${JUPYTER_VENV:-${EXPERIMENT_ROOT}/.venv-jupyter}"
JUPYTER_ROOT_DIR="${JUPYTER_ROOT_DIR:-${REPOSITORY_ROOT}}"
JUPYTER_PORT="${JUPYTER_PORT:-8888}"
CONFIG_HOME="${HOME}/.config/commu-jupyter"
USER_UNIT_HOME="${HOME}/.config/systemd/user"

if [[ ! -d "${JUPYTER_ROOT_DIR}" ]]; then
  echo "JUPYTER_ROOT_DIR does not exist: ${JUPYTER_ROOT_DIR}" >&2
  exit 2
fi
if [[ ! "${JUPYTER_PORT}" =~ ^[0-9]+$ ]] ||
  ((JUPYTER_PORT < 1024 || JUPYTER_PORT > 65535)); then
  echo "JUPYTER_PORT must be an unprivileged TCP port; got ${JUPYTER_PORT}." >&2
  exit 2
fi

python3 -m venv "${JUPYTER_VENV}"
"${JUPYTER_VENV}/bin/python" -m pip install --upgrade pip
"${JUPYTER_VENV}/bin/python" -m pip install \
  -r "${WEB_ROOT}/requirements-jupyter.txt"

umask 077
mkdir -p "${CONFIG_HOME}" "${USER_UNIT_HOME}"
cp "${WEB_ROOT}/jupyter_server_config.py" "${CONFIG_HOME}/jupyter_server_config.py"
if [[ ! -s "${CONFIG_HOME}/token" ]]; then
  openssl rand -hex 32 >"${CONFIG_HOME}/token"
fi
{
  printf 'JUPYTER_VENV=%q\n' "${JUPYTER_VENV}"
  printf 'JUPYTER_ROOT_DIR=%q\n' "${JUPYTER_ROOT_DIR}"
  printf 'JUPYTER_PORT=%q\n' "${JUPYTER_PORT}"
  printf 'JUPYTER_CONFIG_FILE=%q\n' "${CONFIG_HOME}/jupyter_server_config.py"
} >"${CONFIG_HOME}/environment"
cp "${WEB_ROOT}/commu-jupyter.service" \
  "${USER_UNIT_HOME}/commu-jupyter.service"

systemctl --user daemon-reload
systemctl --user enable --now commu-jupyter.service
sleep 2
systemctl --user --no-pager --full status commu-jupyter.service

echo
echo "JupyterLab is listening only at http://127.0.0.1:${JUPYTER_PORT}."
echo "Token file: ${CONFIG_HOME}/token (mode 0600; do not paste it into logs)."
echo "Next: configure one approved HTTPS ingress method from docs/jupyter_web_access.md."
