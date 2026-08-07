#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CADDY_VERSION="${CADDY_VERSION:-2.11.3}"
ARCH="${CADDY_ARCH:-amd64}"
ARCHIVE="caddy_${CADDY_VERSION}_linux_${ARCH}.tar.gz"
CHECKSUMS="caddy_${CADDY_VERSION}_checksums.txt"
BASE_URL="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}"
TOOLS_DIR="${EXPERIMENT_ROOT}/.tools"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TMP_DIR}"' EXIT

mkdir -p "${TOOLS_DIR}"
curl --fail --location --output "${TMP_DIR}/${ARCHIVE}" "${BASE_URL}/${ARCHIVE}"
curl --fail --location --output "${TMP_DIR}/${CHECKSUMS}" "${BASE_URL}/${CHECKSUMS}"
(
  cd "${TMP_DIR}"
  grep "  ${ARCHIVE}\$" "${CHECKSUMS}" | sha512sum --check -
)
tar -xzf "${TMP_DIR}/${ARCHIVE}" -C "${TOOLS_DIR}" caddy
chmod 0755 "${TOOLS_DIR}/caddy"
"${TOOLS_DIR}/caddy" version
