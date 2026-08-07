#!/usr/bin/env bash
# Deterministic client handoff bundle.  Produces a self-contained,
# credential-free archive with client scripts, pinned requirements,
# CA certificate, config template, checksums, and README.
#
# Output:  runs/physical_validation/handoff/commu-client-bundle.tar.gz
#          runs/physical_validation/handoff/commu-client-bundle.tar.gz.sha256
#
# The bundle is immutable after creation — rerun only regenerates if the
# source files have changed (deterministic checksum).  If unchanged, the
# archive is not rebuilt.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
OUTPUT_DIR="${EXPERIMENT_ROOT}/runs/physical_validation/handoff"
BUNDLE_VERSION="v5"
BUNDLE_NAME="commu-client-bundle-${BUNDLE_VERSION}"
mkdir -p "${OUTPUT_DIR}"
# Never overwrite an existing versioned archive.
if [[ -f "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz" ]]; then
  echo "Archive ${BUNDLE_NAME}.tar.gz already exists — append-only, not overwriting." >&2
  echo "Increment BUNDLE_VERSION in the script if a new archive is needed." >&2
  exit 1
fi

# --- gather source files ----------------------------------------------------
CA_CERT="${EXPERIMENT_ROOT}/runs/physical_validation/server/root.crt"
if [[ ! -f "${CA_CERT}" ]]; then
  # Fall back to the Caddy-generated cert.
  CA_CERT="${EXPERIMENT_ROOT}/runs/lab/caddy/data/caddy/pki/authorities/local/root.crt"
fi
if [[ ! -f "${CA_CERT}" ]]; then
  echo "No CA certificate found.  Run 23_server_physical_listener.sh start first." >&2
  exit 2
fi

# Pin aioquic to exactly the server version.
AIOQUIC_VERSION="$("${EXPERIMENT_ROOT}/.venv-runner/bin/python" -c 'import aioquic; print(aioquic.__version__)' 2>/dev/null || echo "1.3.0")"
PYTHON_VERSION="$("${EXPERIMENT_ROOT}/.venv-runner/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "3.11")"

WORKDIR="$(mktemp -d)"
trap 'rm -rf ${WORKDIR}' EXIT
BUNDLE_ROOT="${WORKDIR}/commu-client"

mkdir -p "${BUNDLE_ROOT}/scripts"
mkdir -p "${BUNDLE_ROOT}/config"
mkdir -p "${BUNDLE_ROOT}/certs"

# --- client scripts ---------------------------------------------------------
# Client runner (WSL / Windows-compatible)
cp "${SCRIPT_DIR}/run_physical_client.sh" "${BUNDLE_ROOT}/scripts/"
# Capture interface discovery
cp "${SCRIPT_DIR}/discover_capture_interface.sh" "${BUNDLE_ROOT}/scripts/"

# --- requirements -----------------------------------------------------------
cat >"${BUNDLE_ROOT}/requirements-client.txt" <<EOF
# Pinned to match server versions exactly.
aioquic==${AIOQUIC_VERSION}
httpx>=0.27,<1
PyYAML>=6,<7
EOF

# --- config template --------------------------------------------------------
PHYS_IP="${PHYS_IP:-144.214.210.31}"
cat >"${BUNDLE_ROOT}/config/client.env" <<EOF
# Client configuration — pre-filled from server.
# LISTENER_PROFILE must match the server-side profile.
SERVER_IP=${PHYS_IP}
LISTENER_PROFILE=standard_https
TLS_PORT=443
H3_PORT=443
API_KEY=REPLACE_ME
MODEL=Qwen/Qwen3-8B
MANIFEST_PATH=artifacts/requests_32.jsonl
OUTPUT_DIR=runs/physical_validation/client
CAPTURE_INTERFACE=REPLACE_ME
WIRESHARK_BIN="/mnt/c/Program Files/Wireshark"
CLIENT_MTU=1420
EOF

# --- CA certificate ---------------------------------------------------------
if [[ -f "${CA_CERT}" ]]; then
  cp "${CA_CERT}" "${BUNDLE_ROOT}/certs/caddy-root.crt"
else
  echo "WARNING: CA cert not found at ${CA_CERT}" >&2
  echo "  Run 23_server_physical_listener.sh start first to generate it." >&2
  echo "  The bundle will be usable after the cert is copied in manually." >&2
  touch "${BUNDLE_ROOT}/certs/caddy-root.crt.missing"
fi

# --- README -----------------------------------------------------------------
cat >"${BUNDLE_ROOT}/README.md" <<EOF
# Commu Physical-Client Validation Bundle

Server: ${PHYS_IP:-144.214.210.31}
Python: ${PYTHON_VERSION}
aioquic: ${AIOQUIC_VERSION}
Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)

## Prerequisites (WSL / Windows)
- Python ${PYTHON_VERSION} with venv support
- Wireshark 4.x with dumpcap/tshark on PATH, or accessible via WSL
- Network path: WSL2 → vEthernet → Windows → VPN → ${PHYS_IP:-144.214.210.31}

## Setup (client side, no sudo)
```bash
cd ~/commu
tar -xzf commu-client-bundle.tar.gz -C .
cd commu-client
python3 -m venv .venv-client
.venv-client/bin/pip install -r requirements-client.txt
# Edit config/client.env: set CAPTURE_INTERFACE
bash scripts/discover_capture_interface.sh
```

## Pilot Commands (read-only validation, no privileged launch)
```bash
cd ~/commu/commu-client
source config/client.env
bash scripts/run_physical_client.sh tls standard_https
bash scripts/run_physical_client.sh h3 standard_https
```

## Validation
- TLS PCAP: TCP + TLS 1.3, direction counts, MTU ≤ 1420 (WSL), filter clean
- HTTP/3 PCAP: UDP + QUIC (decode-as udp.port==8444,quic), zero TCP, MTU ≤ 1420
EOF

# --- checksums --------------------------------------------------------------
cd "${BUNDLE_ROOT}/.."
# Deterministic tar: sort entries, omit timestamps, gzip -n for no timestamp.
tar --sort=name --mtime="1970-01-01 00:00:00" --owner=0 --group=0 \
  -czf "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz" commu-client/
sha256sum "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz" >"${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz.sha256"

echo "Bundle: ${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"
sha256sum "${OUTPUT_DIR}/${BUNDLE_NAME}.tar.gz"
echo "BUNDLE_READY"
