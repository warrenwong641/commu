#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
OUTPUT="${1:-${REPOSITORY_ROOT}/commu-lab-transfer.tar.gz}"
QA_MANIFEST="${EXPERIMENT_ROOT}/artifacts/requests_32.jsonl"
SUMMARY_MANIFEST="${EXPERIMENT_ROOT}/artifacts/event_summaries_10.jsonl"

case "${OUTPUT}" in
  /*) ;;
  *) OUTPUT="${PWD}/${OUTPUT}" ;;
esac

for output_path in "${OUTPUT}" "${OUTPUT}.sha256" "${OUTPUT}.manifests.sha256"; do
  if [[ -e "${output_path}" || -L "${output_path}" ]]; then
    echo "Refusing to overwrite existing bundle artifact: ${output_path}" >&2
    exit 2
  fi
done
if [[ ! -f "${QA_MANIFEST}" || ! -f "${SUMMARY_MANIFEST}" ]]; then
  echo "The frozen manifests are not present in this checkout:" >&2
  echo "  ${QA_MANIFEST}" >&2
  echo "  ${SUMMARY_MANIFEST}" >&2
  echo "Copy them from AutoDL before creating the final lab bundle." >&2
  exit 2
fi

mkdir -p "$(dirname -- "${OUTPUT}")"
tar -czf "${OUTPUT}" \
  --exclude='.git' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='tmp' \
  --exclude='output' \
  --exclude='*.pdf' \
  --exclude='*.pcap' \
  --exclude='*.pcapng' \
  --exclude='*.log' \
  --exclude='traffic_experiment/.venv-*' \
  --exclude='traffic_experiment/.tools' \
  --exclude='traffic_experiment/runs' \
  --exclude='traffic_experiment/server.env' \
  -C "${REPOSITORY_ROOT}" \
  traffic_experiment locomo_eval requirements.txt

sha256sum "${OUTPUT}" >"${OUTPUT}.sha256"
(
  cd "${EXPERIMENT_ROOT}"
  sha256sum artifacts/requests_32.jsonl artifacts/event_summaries_10.jsonl
) >"${OUTPUT}.manifests.sha256"
QA_SHA256="$(sha256sum "${QA_MANIFEST}" | awk '{print $1}')"
SUMMARY_SHA256="$(sha256sum "${SUMMARY_MANIFEST}" | awk '{print $1}')"
echo "Created ${OUTPUT}"
echo "Checksum: ${OUTPUT}.sha256"
echo "Manifest checksums: ${OUTPUT}.manifests.sha256"
echo "MANIFEST_SHA256=${QA_SHA256}"
echo "SUMMARY_MANIFEST_SHA256=${SUMMARY_SHA256}"
echo "The bundle excludes credentials, packet captures, run outputs, model caches, and PDFs."
