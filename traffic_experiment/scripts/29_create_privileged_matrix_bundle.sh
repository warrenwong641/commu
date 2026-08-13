#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

# Build a credential-free, checksum-verifiable release for the fixed full
# matrix. Run this as the service user. Root only consumes the finished archive
# through 30_install_privileged_matrix_release.sh.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
CONFIG=""
QA_MANIFEST=""
SUMMARY_MANIFEST=""
RUNNER_WHEELHOUSE=""
CADDY_BIN="${EXPERIMENT_ROOT}/.tools/caddy"
SERVICE_STATE=""
SERVICE_USER=""
SERVICE_UID=""
SERVICE_GID=""
EXPECTED_GPU_UUID=""
OUTPUT=""

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
usage() {
  cat <<'EOF'
Usage: 29_create_privileged_matrix_bundle.sh \
  --config PATH --qa-manifest PATH --summary-manifest PATH \
  --service-state PATH --service-user NAME --service-uid UID --service-gid GID \
  --gpu-uuid GPU-UUID --output PATH --wheelhouse PATH [--caddy-bin PATH]

The config may use @REPOSITORY_SHA@ in RUNS_ROOT and CADDY_RUN_DIR. The bundle
contains no API key and can launch only the fixed one-GPU 2,808-call matrix.
EOF
}

while (($#)); do
  case "$1" in
    --config) [[ $# -ge 2 ]] || die "--config requires a path"; CONFIG="$2"; shift 2 ;;
    --qa-manifest) [[ $# -ge 2 ]] || die "--qa-manifest requires a path"; QA_MANIFEST="$2"; shift 2 ;;
    --summary-manifest) [[ $# -ge 2 ]] || die "--summary-manifest requires a path"; SUMMARY_MANIFEST="$2"; shift 2 ;;
    --wheelhouse) [[ $# -ge 2 ]] || die "--wheelhouse requires a path"; RUNNER_WHEELHOUSE="$2"; shift 2 ;;
    --caddy-bin) [[ $# -ge 2 ]] || die "--caddy-bin requires a path"; CADDY_BIN="$2"; shift 2 ;;
    --service-state) [[ $# -ge 2 ]] || die "--service-state requires a path"; SERVICE_STATE="$2"; shift 2 ;;
    --service-user) [[ $# -ge 2 ]] || die "--service-user requires a name"; SERVICE_USER="$2"; shift 2 ;;
    --service-uid) [[ $# -ge 2 ]] || die "--service-uid requires a UID"; SERVICE_UID="$2"; shift 2 ;;
    --service-gid) [[ $# -ge 2 ]] || die "--service-gid requires a GID"; SERVICE_GID="$2"; shift 2 ;;
    --gpu-uuid) [[ $# -ge 2 ]] || die "--gpu-uuid requires a UUID"; EXPECTED_GPU_UUID="$2"; shift 2 ;;
    --output) [[ $# -ge 2 ]] || die "--output requires a path"; OUTPUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${EUID}" -ne 0 ]] || die "build the release as the unprivileged service user"
for value in CONFIG QA_MANIFEST SUMMARY_MANIFEST SERVICE_STATE SERVICE_USER \
  SERVICE_UID SERVICE_GID EXPECTED_GPU_UUID OUTPUT RUNNER_WHEELHOUSE; do
  [[ -n "${!value}" ]] || die "missing required option for ${value}"
done
for command_name in git tar gzip cp find sha256sum awk mktemp readlink; do
  command -v "${command_name}" >/dev/null 2>&1 || die "missing command: ${command_name}"
done
PYTHON="$(command -v python3 || true)"
[[ -n "${PYTHON}" && -x "${PYTHON}" ]] || die "python3 is required"

canonical_regular() {
  local resolved
  [[ -f "$1" && ! -L "$1" ]] || return 1
  resolved="$(readlink -e -- "$1")" || return 1
  printf '%s\n' "${resolved}"
}
canonical_directory() {
  local resolved
  [[ -d "$1" && ! -L "$1" ]] || return 1
  resolved="$(readlink -e -- "$1")" || return 1
  printf '%s\n' "${resolved}"
}

CONFIG="$(canonical_regular "${CONFIG}")" || die "unsafe config path"
QA_MANIFEST="$(canonical_regular "${QA_MANIFEST}")" || die "unsafe QA manifest path"
SUMMARY_MANIFEST="$(canonical_regular "${SUMMARY_MANIFEST}")" || die "unsafe summary manifest path"
RUNNER_WHEELHOUSE="$(canonical_directory "${RUNNER_WHEELHOUSE}")" || die "unsafe wheelhouse path"
CADDY_BIN="$(canonical_regular "${CADDY_BIN}")" || die "unsafe Caddy path"
[[ -x "${CADDY_BIN}" ]] || die "Caddy binary is not executable"
EXPECTED_CADDY_SHA256="f16be85d67d7a8369c7255a8514204112bb63a58490bba7d458b38818a75fb94"
[[ "$(sha256sum -- "${CADDY_BIN}" | awk '{print $1}')" == "${EXPECTED_CADDY_SHA256}" ]] ||
  die "Caddy binary does not match the independently pinned release digest"
[[ "${SERVICE_STATE}" = /* && "${SERVICE_STATE}" != *$'\n'* ]] || die "service state must be an absolute single-line path"
[[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unsafe service user"
[[ "${SERVICE_UID}" =~ ^[1-9][0-9]*$ && "${SERVICE_GID}" =~ ^[1-9][0-9]*$ ]] || die "service UID/GID must be positive integers"
[[ "${EXPECTED_GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ ]] || die "invalid expected GPU UUID"

REPOSITORY_SHA="$(git -C "${REPOSITORY_ROOT}" rev-parse HEAD)"
[[ "${REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "could not resolve an exact repository commit"
git -C "${REPOSITORY_ROOT}" diff --quiet --ignore-submodules -- || die "tracked worktree changes must be committed first"
git -C "${REPOSITORY_ROOT}" diff --cached --quiet --ignore-submodules -- || die "staged changes must be committed first"

case "${OUTPUT}" in
  /*) ;;
  *) OUTPUT="${PWD}/${OUTPUT}" ;;
esac
OUTPUT_PARENT="$(dirname -- "${OUTPUT}")"
mkdir -p "${OUTPUT_PARENT}"
OUTPUT_PARENT="$(canonical_directory "${OUTPUT_PARENT}")" || die "unsafe output parent"
OUTPUT="${OUTPUT_PARENT}/$(basename -- "${OUTPUT}")"
for path in "${OUTPUT}" "${OUTPUT}.sha256"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || die "refusing to overwrite ${path}"
done

BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/commu-matrix-release.XXXXXX")"
cleanup() {
  local status=$?
  trap - EXIT
  [[ "${BUILD_ROOT}" == "${TMPDIR:-/tmp}"/commu-matrix-release.* ]] &&
    rm -rf -- "${BUILD_ROOT}"
  exit "${status}"
}
trap cleanup EXIT
RELEASE_ROOT="${BUILD_ROOT}/release"
mkdir -p "${RELEASE_ROOT}/repository" "${RELEASE_ROOT}/config" "${RELEASE_ROOT}/policy"

git -c core.autocrlf=false -c core.eol=lf \
  -C "${REPOSITORY_ROOT}" archive --format=tar "${REPOSITORY_SHA}" |
  tar -xf - -C "${RELEASE_ROOT}/repository"

# This manifest is the independently attestable reviewed-code anchor. The
# installer script is excluded to avoid a self-referential digest; its own
# published SHA-256 is the trust anchor for the embedded expected manifest
# digest. Dynamic experiment artifacts and pinned third-party binaries/runtime
# archives cross the boundary through their separate validation paths.
(
  cd "${RELEASE_ROOT}"
  find repository -type f \
    ! -path 'repository/traffic_experiment/artifacts/*' \
    ! -path 'repository/traffic_experiment/scripts/30_install_privileged_matrix_release.sh' \
    -print0 | LC_ALL=C sort -z | xargs -0 sha256sum --text -- \
    >REVIEWED_CODE_FILES.sha256
)

"${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" normalize \
  --input "${CONFIG}" \
  --output "${RELEASE_ROOT}/config/server.env" \
  --repository-sha "${REPOSITORY_SHA}"

QA_RELATIVE="$(
  "${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" get \
    --input "${RELEASE_ROOT}/config/server.env" \
    --repository-sha "${REPOSITORY_SHA}" --key MANIFEST_PATH
)"
SUMMARY_RELATIVE="$(
  "${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" get \
    --input "${RELEASE_ROOT}/config/server.env" \
    --repository-sha "${REPOSITORY_SHA}" --key SUMMARY_MANIFEST_PATH
)"
mkdir -p \
  "$(dirname -- "${RELEASE_ROOT}/repository/traffic_experiment/${QA_RELATIVE}")" \
  "$(dirname -- "${RELEASE_ROOT}/repository/traffic_experiment/${SUMMARY_RELATIVE}")"
cp -- "${QA_MANIFEST}" "${RELEASE_ROOT}/repository/traffic_experiment/${QA_RELATIVE}"
cp -- "${SUMMARY_MANIFEST}" "${RELEASE_ROOT}/repository/traffic_experiment/${SUMMARY_RELATIVE}"

QA_EXPECTED="$(
  "${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" get \
    --input "${RELEASE_ROOT}/config/server.env" \
    --repository-sha "${REPOSITORY_SHA}" --key MANIFEST_SHA256
)"
SUMMARY_EXPECTED="$(
  "${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" get \
    --input "${RELEASE_ROOT}/config/server.env" \
    --repository-sha "${REPOSITORY_SHA}" --key SUMMARY_MANIFEST_SHA256
)"
[[ "$(sha256sum -- "${QA_MANIFEST}" | awk '{print $1}')" == "${QA_EXPECTED,,}" ]] || die "QA manifest digest differs from config"
[[ "$(sha256sum -- "${SUMMARY_MANIFEST}" | awk '{print $1}')" == "${SUMMARY_EXPECTED,,}" ]] || die "summary manifest digest differs from config"

# Root rebuilds the runner offline from this wheelhouse, with every selected
# distribution constrained by the committed minimal runtime lock hashes.
WHEEL_MANIFEST="${EXPERIMENT_ROOT}/privileged-pilot-wheels.cp312-linux-x86_64.sha256"
[[ -f "${WHEEL_MANIFEST}" && ! -L "${WHEEL_MANIFEST}" ]] || die "wheel manifest is missing"
"${PYTHON}" - "${RUNNER_WHEELHOUSE}" "${WHEEL_MANIFEST}" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

wheelhouse = Path(sys.argv[1])
manifest = Path(sys.argv[2])
expected = {}
for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+\.whl)", line)
    if not match or match.group(2) in expected:
        raise SystemExit(f"invalid wheel manifest line {number}")
    expected[match.group(2)] = match.group(1)
entries = list(wheelhouse.iterdir())
actual = {path.name: path for path in entries}
if (
    set(actual) != set(expected)
    or any(path.is_symlink() or not path.is_file() for path in entries)
):
    raise SystemExit("wheelhouse filenames/coverage differ from the CPython 3.12 Linux manifest")
for name, path in actual.items():
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
        raise SystemExit(f"wheel digest mismatch: {name}")
PY
cp -aL -- "${RUNNER_WHEELHOUSE}" "${RELEASE_ROOT}/wheelhouse"
mkdir -p "${RELEASE_ROOT}/repository/traffic_experiment/.tools"
cp -- "${CADDY_BIN}" "${RELEASE_ROOT}/repository/traffic_experiment/.tools/caddy"
chmod 0555 "${RELEASE_ROOT}/repository/traffic_experiment/.tools/caddy"
if ! find "${RELEASE_ROOT}/wheelhouse" -mindepth 1 -maxdepth 1 -type f \
  -name '*.whl' -print -quit | grep -q .; then
  die "wheelhouse contains no wheels"
fi
if find "${RELEASE_ROOT}/wheelhouse" -mindepth 1 \
  ! -type f -o -type f ! -name '*.whl' | grep -q .; then
  die "wheelhouse must contain regular wheel files only"
fi
if find "${RELEASE_ROOT}" -type l -print -quit | grep -q .; then
  die "release staging unexpectedly contains a symbolic link"
fi

cat >"${RELEASE_ROOT}/policy/service.state" <<EOF
schema=commu-privileged-matrix-policy-v1
repository_sha=${REPOSITORY_SHA}
service_state=${SERVICE_STATE}
service_user=${SERVICE_USER}
service_uid=${SERVICE_UID}
service_gid=${SERVICE_GID}
expected_gpu_uuid=${EXPECTED_GPU_UUID}
pilot_repository_sha=7ed49eb0a04c3d4bd69e7361aab31de83426c61f
EOF
cat >"${RELEASE_ROOT}/RELEASE_METADATA" <<EOF
schema=commu-privileged-matrix-release-v1
repository_sha=${REPOSITORY_SHA}
purpose=secure-single-gpu-full-matrix
EOF

"${PYTHON}" "${SCRIPT_DIR}/privileged_matrix_config.py" check \
  --input "${RELEASE_ROOT}/config/server.env" \
  --repository-sha "${REPOSITORY_SHA}" \
  --release-root "${RELEASE_ROOT}"

(
  cd "${RELEASE_ROOT}"
  find . -type f ! -name RELEASE_FILES.sha256 -print0 |
    LC_ALL=C sort -z |
    xargs -0 sha256sum --text -- >RELEASE_FILES.sha256
  sha256sum --check --strict --quiet RELEASE_FILES.sha256
)

ARCHIVE_TMP="$(mktemp "${OUTPUT_PARENT}/.$(basename -- "${OUTPUT}").XXXXXX")"
rm -- "${ARCHIVE_TMP}"
(
  cd "${BUILD_ROOT}"
  LC_ALL=C tar --sort=name --hard-dereference --mtime='UTC 1970-01-01' \
    --owner=0 --group=0 --numeric-owner -cf - release |
    gzip -n >"${ARCHIVE_TMP}"
)
chmod 0600 "${ARCHIVE_TMP}"
mv -- "${ARCHIVE_TMP}" "${OUTPUT}"
sha256sum -- "${OUTPUT}" >"${OUTPUT}.sha256"

printf 'PRIVILEGED_MATRIX_BUNDLE_OK repository_sha=%s\n' "${REPOSITORY_SHA}"
printf 'bundle=%s\n' "${OUTPUT}"
printf 'bundle_sha256=%s\n' "$(sha256sum -- "${OUTPUT}" | awk '{print $1}')"
printf 'contains_credentials=false\n'
printf 'purpose=secure-single-gpu-full-matrix\n'
