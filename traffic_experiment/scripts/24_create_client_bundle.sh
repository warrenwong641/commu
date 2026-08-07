#!/usr/bin/env bash
# Build a credential-free, self-contained physical-client archive.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
OUTPUT_DIR="${CLIENT_BUNDLE_OUTPUT_DIR:-${EXPERIMENT_ROOT}/runs/physical_validation/handoff}"
BUNDLE_VERSION="${CLIENT_BUNDLE_VERSION:-v6}"
BUNDLE_NAME="commu-client-bundle-${BUNDLE_VERSION}"
OUTPUT="${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"

QA_MANIFEST="${CLIENT_QA_MANIFEST:-${EXPERIMENT_ROOT}/artifacts/requests_32.jsonl}"
SUMMARY_MANIFEST="${CLIENT_SUMMARY_MANIFEST:-}"
CLIENT_LISTENER_PROFILE="${CLIENT_LISTENER_PROFILE:-standard_https}"
case "${CLIENT_LISTENER_PROFILE}" in
  standard_https | high_ports) ;;
  *) echo "CLIENT_LISTENER_PROFILE must be standard_https or high_ports." >&2; exit 2 ;;
esac
CA_CERT="${CLIENT_CA_CERT:-${EXPERIMENT_ROOT}/runs/physical_validation/server/${CLIENT_LISTENER_PROFILE}/root.crt}"
if [[ ! -f "${CA_CERT}" ]]; then
  CA_CERT="${EXPERIMENT_ROOT}/runs/lab/caddy/data/caddy/pki/authorities/local/root.crt"
fi
CLIENT_MODEL="${CLIENT_MODEL:-${VLLM_SERVED_MODEL_NAME:-}}"

if [[ ! -s "${QA_MANIFEST}" ]]; then
  echo "Frozen QA manifest is required and must be nonempty: ${QA_MANIFEST}" >&2
  echo "Set CLIENT_QA_MANIFEST to the exact frozen requests JSONL artifact." >&2
  exit 2
fi
if [[ -n "${SUMMARY_MANIFEST}" && ! -s "${SUMMARY_MANIFEST}" ]]; then
  echo "CLIENT_SUMMARY_MANIFEST was set but is missing or empty: ${SUMMARY_MANIFEST}" >&2
  exit 2
fi
if [[ ! -s "${CA_CERT}" ]]; then
  echo "CA certificate is required: set CLIENT_CA_CERT or start the physical listener." >&2
  exit 2
fi
if [[ -z "${CLIENT_MODEL}" ]]; then
  echo "Set CLIENT_MODEL to the exact model name served by vLLM." >&2
  exit 2
fi
if [[ ! -d "${EXPERIMENT_ROOT}/traffic_measure" || ! -d "${REPOSITORY_ROOT}/locomo_eval" ]]; then
  echo "Required Python source trees are absent; run this from the complete repository." >&2
  exit 2
fi

# Refuse a malformed or unfrozen-looking input before constructing the archive.
python3 - "${QA_MANIFEST}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rows = [
    json.loads(line)
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
if not rows:
    raise SystemExit("frozen QA manifest contains no rows")
if not any(row.get("condition") == "no_compression" for row in rows):
    raise SystemExit("frozen QA manifest has no no_compression row")
required = {"request_id", "sample_id", "condition", "messages", "messages_sha256"}
for index, row in enumerate(rows, start=1):
    missing = sorted(required - row.keys())
    if missing:
        raise SystemExit(f"manifest row {index} is missing {missing}")
print(f"manifest_rows={len(rows)} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
PY

mkdir -p "${OUTPUT_DIR}"
if [[ -e "${OUTPUT}" || -e "${OUTPUT}.sha256" ]]; then
  echo "Refusing to overwrite existing versioned bundle: ${OUTPUT}" >&2
  echo "Increment CLIENT_BUNDLE_VERSION for a new immutable archive." >&2
  exit 1
fi

RUNNER_PYTHON="${RUNNER_PYTHON:-${EXPERIMENT_ROOT}/.venv-runner/bin/python}"
if [[ -x "${RUNNER_PYTHON}" ]]; then
  AIOQUIC_VERSION="$("${RUNNER_PYTHON}" -c 'import aioquic; print(aioquic.__version__)')"
  PYTHON_VERSION="$("${RUNNER_PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
else
  echo "Runner Python is required to pin the client aioquic/Python versions: ${RUNNER_PYTHON}" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
cleanup() {
  local target
  target="$(cd -- "${WORKDIR}" && pwd)"
  case "${target}" in
    /tmp/* | /var/tmp/*) rm -rf -- "${target}" ;;
    *) echo "Refusing to remove unexpected temporary path: ${target}" >&2 ;;
  esac
}
trap cleanup EXIT

BUNDLE_ROOT="${WORKDIR}/commu-client"
mkdir -p \
  "${BUNDLE_ROOT}/config" \
  "${BUNDLE_ROOT}/traffic_experiment/artifacts" \
  "${BUNDLE_ROOT}/traffic_experiment/certs" \
  "${BUNDLE_ROOT}/traffic_experiment/scripts" \
  "${BUNDLE_ROOT}/traffic_experiment/traffic_measure"

cp -- "${SCRIPT_DIR}/run_physical_client.sh" \
  "${BUNDLE_ROOT}/traffic_experiment/scripts/"
cp -- "${SCRIPT_DIR}/discover_capture_interface.sh" \
  "${BUNDLE_ROOT}/traffic_experiment/scripts/"
cp -- "${QA_MANIFEST}" \
  "${BUNDLE_ROOT}/traffic_experiment/artifacts/requests_32.jsonl"
cp -- "${CA_CERT}" \
  "${BUNDLE_ROOT}/traffic_experiment/certs/caddy-root.crt"
if [[ -n "${SUMMARY_MANIFEST}" ]]; then
  cp -- "${SUMMARY_MANIFEST}" \
    "${BUNDLE_ROOT}/traffic_experiment/artifacts/event_summaries.jsonl"
fi

# The traffic CLI imports prepare.py eagerly, and prepare.py imports locomo_eval.
# Include both source trees so extraction plus dependency installation is enough.
while IFS= read -r source; do
  relative="${source#${REPOSITORY_ROOT}/}"
  destination="${BUNDLE_ROOT}/${relative}"
  mkdir -p "$(dirname -- "${destination}")"
  cp -- "${source}" "${destination}"
done < <(
  find "${EXPERIMENT_ROOT}/traffic_measure" "${REPOSITORY_ROOT}/locomo_eval" \
    -type f -name '*.py' -print | sort
)

cat >"${BUNDLE_ROOT}/requirements-client.txt" <<EOF
# Pinned to the server's installed HTTP/3 implementation.
aioquic==${AIOQUIC_VERSION}
httpx>=0.27,<1
PyYAML>=6,<7
rouge-score>=0.1.2,<1
EOF

{
  printf '%s\n' \
    '# Set only non-secret client configuration here.' \
    '# Supply LOCAL_VLLM_API_KEY interactively; never save it in this file.'
  printf 'SERVER_IP=%q\n' "${PHYS_IP:-144.214.210.31}"
  printf 'LISTENER_PROFILE=%q\n' "${CLIENT_LISTENER_PROFILE}"
  printf 'MODEL=%q\n' "${CLIENT_MODEL}"
  printf 'MANIFEST_PATH=%q\n' "traffic_experiment/artifacts/requests_32.jsonl"
  printf 'OUTPUT_DIR=%q\n' "traffic_experiment/runs/physical_validation/client"
  printf 'CA_CERT=%q\n' "traffic_experiment/certs/caddy-root.crt"
  printf 'CAPTURE_INTERFACE=%q\n' "REPLACE_ME"
  printf 'WIRESHARK_BIN=%q\n' "/mnt/c/Program Files/Wireshark"
  printf 'CLIENT_MTU=%q\n' "1420"
  printf 'CLIENT_PYTHON=%q\n' ".venv-client/bin/python"
} >"${BUNDLE_ROOT}/config/client.env"

cat >"${BUNDLE_ROOT}/README.md" <<EOF
# Commu physical-client validation bundle

This archive contains the runner source, its \`locomo_eval\` import dependency,
the exact frozen QA manifest, the server CA certificate, and pinned client
requirements. It contains no API key.

Server: ${PHYS_IP:-144.214.210.31}
Model: ${CLIENT_MODEL}
Python: ${PYTHON_VERSION}
aioquic: ${AIOQUIC_VERSION}

## Setup

\`\`\`bash
tar -xzf ${BUNDLE_NAME}.tar.gz
cd commu-client
python${PYTHON_VERSION} -m venv .venv-client
.venv-client/bin/pip install -r requirements-client.txt
# Edit config/client.env and set CAPTURE_INTERFACE.
set -a
source config/client.env
set +a
read -rsp "vLLM API key: " LOCAL_VLLM_API_KEY
printf '\n'
export LOCAL_VLLM_API_KEY
\`\`\`

## One-request protocol pilots

\`\`\`bash
.venv-client/bin/python -c "import aioquic, httpx, locomo_eval"
bash traffic_experiment/scripts/run_physical_client.sh tls "\${LISTENER_PROFILE}"
bash traffic_experiment/scripts/run_physical_client.sh h3 "\${LISTENER_PROFILE}"
\`\`\`

The aliases \`tls13\` and \`http3\` are also accepted. Calibration and endpoint
checks run before capture. Every failed or interrupted attempt remains in its
own \`attempt-NNN\` directory; only a fully validated capture gets a
\`VALIDATION_COMPLETE\` marker.
EOF

(
  cd "${BUNDLE_ROOT}"
  sha256sum \
    traffic_experiment/artifacts/requests_32.jsonl \
    traffic_experiment/certs/caddy-root.crt \
    >FROZEN_ARTIFACTS.sha256
  if [[ -f traffic_experiment/artifacts/event_summaries.jsonl ]]; then
    sha256sum traffic_experiment/artifacts/event_summaries.jsonl \
      >>FROZEN_ARTIFACTS.sha256
  fi
)

# Normalize archive metadata and suppress gzip timestamps for reproducibility.
(
  cd "${WORKDIR}"
  tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner \
    -cf - commu-client |
    gzip -n >"${OUTPUT}"
)
(
  cd "${OUTPUT_DIR}"
  sha256sum "${BUNDLE_NAME}.tar.gz" >"${BUNDLE_NAME}.tar.gz.sha256"
)

echo "Bundle: ${OUTPUT}"
echo "Checksum: ${OUTPUT}.sha256"
echo "BUNDLE_READY"
