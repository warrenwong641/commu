#!/usr/bin/bash -p
set -euo pipefail
set +x
umask 077

# Bootstrap installer. Copy this file to a root-owned path and verify its
# published SHA-256 before executing it. It never runs code from the source
# checkout and opens the unprivileged bundle only for a pinned-byte copy.

FIXED_PATH=/usr/sbin:/usr/bin
if [[ "${COMMU_PRIVILEGED_PILOT_INSTALL_CLEAN_ENV:-}" != 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'ERROR: installer must run as root\n' >&2; exit 2; }
  BOOTSTRAP_SELF="$(/usr/bin/readlink -e -- "$0")" || exit 2
  [[ -f "${BOOTSTRAP_SELF}" && ! -L "${BOOTSTRAP_SELF}" &&
    "$(/usr/bin/stat -c %u:%h -- "${BOOTSTRAP_SELF}")" == 0:1 ]] || exit 2
  BOOTSTRAP_MODE="$(/usr/bin/stat -c %a -- "${BOOTSTRAP_SELF}")"
  (( (8#${BOOTSTRAP_MODE} & 8#022) == 0 )) || exit 2
  BOOTSTRAP_PARENT="$(/usr/bin/readlink -e -- "$(/usr/bin/dirname -- "${BOOTSTRAP_SELF}")")" || exit 2
  [[ -d "${BOOTSTRAP_PARENT}" && ! -L "${BOOTSTRAP_PARENT}" &&
    "$(/usr/bin/stat -c %u -- "${BOOTSTRAP_PARENT}")" == 0 ]] || exit 2
  BOOTSTRAP_PARENT_MODE="$(/usr/bin/stat -c %a -- "${BOOTSTRAP_PARENT}")"
  (( (8#${BOOTSTRAP_PARENT_MODE} & 8#022) == 0 )) || exit 2
  exec /usr/bin/env -i \
    COMMU_PRIVILEGED_PILOT_INSTALL_CLEAN_ENV=1 \
    HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
    /usr/bin/bash -p "${BOOTSTRAP_SELF}" "$@"
fi
PATH="${FIXED_PATH}"
export PATH HOME LANG LC_ALL TZ PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE PYTHONSAFEPATH
while IFS='=' read -r inherited_name _; do
  case "${inherited_name}" in
    COMMU_PRIVILEGED_PILOT_INSTALL_CLEAN_ENV|HOME|LANG|LC_ALL|TZ|PATH|PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|PYTHONSAFEPATH|PWD|SHLVL|_) ;;
    *) printf 'ERROR: unsanitized installer environment variable: %s\n' "${inherited_name}" >&2; exit 2 ;;
  esac
done < <(/usr/bin/env)
cd /
[[ "${EUID}" -eq 0 && "${HOME}" == /root && "${LANG}" == C.UTF-8 &&
  "${LC_ALL}" == C.UTF-8 && "${TZ}" == UTC && "${PATH}" == "${FIXED_PATH}" &&
  "${PYTHONNOUSERSITE}" == 1 && "${PYTHONDONTWRITEBYTECODE}" == 1 &&
  "${PYTHONSAFEPATH}" == 1 && "${PWD}" == / ]] || {
  printf 'ERROR: sanitized installer environment values do not match policy\n' >&2
  exit 2
}
BASE=/opt/commu-protocol-pilots/releases
OUTPUT_BASE=/var/lib/commu-protocol-pilots
EXPECTED_CADDY_SHA256=f16be85d67d7a8369c7255a8514204112bb63a58490bba7d458b38818a75fb94
EXPECTED_CADDY_VERSION='v2.11.3 h1:/vFbdjcs2DtzcWTIxHybf5R5TspYFFThlZffChyBFHg='
EXPECTED_SERVICE_USER=wongshingyin
EXPECTED_SERVICE_UID=1007
EXPECTED_SERVICE_GID=1007
EXPECTED_SERVICE_STATE_ROOT=/home/wongshingyin/.config/commu
EXPECTED_MODEL=Qwen/Qwen3.5-9B
EXPECTED_SERVED_MODEL=Qwen/Qwen3.5-9B
EXPECTED_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
EXPECTED_QA_MANIFEST=artifacts/requests_32.jsonl
EXPECTED_QA_SHA256=2b4a3de0bd1cc91a9797b9f128b7677a5aa8b64da96b5115982593fac54eed49
EXPECTED_SUMMARY_MANIFEST=artifacts/event_summaries_10.jsonl
EXPECTED_SUMMARY_SHA256=f6873ec918d63c9b8d12aa17efaa460c7b1650977d73bd4585670a3e82b5ca95
# Updated after committing by hashing REVIEWED_CODE_FILES.sha256 from a clean
# git archive. This file itself is excluded; administrators authenticate this
# installer with its separately published SHA-256.
EXPECTED_REVIEWED_CODE_MANIFEST_SHA256=3e95ee0c3b23de284b2266dee0fe97d55fba4432c3a5401511f1c7bc3e2f8ba8
ARCHIVE="${1:-}"
EXPECTED_ARCHIVE_SHA="${2:-}"
EXPECTED_REPOSITORY_SHA="${3:-}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
usage() {
  printf 'Usage: %s BUNDLE.tar.gz BUNDLE_SHA256 REPOSITORY_SHA\n' "$0" >&2
  exit 2
}

[[ "${EUID}" -eq 0 ]] || die "installer must run as root"
[[ $# -eq 3 ]] || usage
[[ "${EXPECTED_ARCHIVE_SHA}" =~ ^[0-9a-f]{64}$ ]] || die "bundle SHA-256 must be lowercase hexadecimal"
[[ "${EXPECTED_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "repository SHA must be lowercase hexadecimal"

for trusted_dir in / /home /opt /usr /usr/bin /usr/sbin /var /var/lib /run; do
  [[ -d "${trusted_dir}" && ! -L "${trusted_dir}" &&
    "$(/usr/bin/readlink -e -- "${trusted_dir}")" == "${trusted_dir}" &&
    "$(/usr/bin/stat -c %u -- "${trusted_dir}")" == 0 ]] ||
    die "unsafe privileged path component: ${trusted_dir} must be canonical and root-owned"
  trusted_mode="$(/usr/bin/stat -c %a -- "${trusted_dir}")"
  (( (8#${trusted_mode} & 8#022) == 0 )) ||
    die "unsafe privileged path component: ${trusted_dir} is group/world writable"
done
[[ -d /run/lock && ! -L /run/lock &&
  "$(/usr/bin/readlink -e -- /run/lock)" == /run/lock &&
  "$(/usr/bin/stat -c %u:%g:%a -- /run/lock)" == 0:0:1777 ]] ||
  die "/run/lock must be the canonical root:root sticky directory (01777)"

SELF="$(/usr/bin/readlink -e -- "$0")" || die "cannot resolve installer path"
[[ -f "${SELF}" && ! -L "${SELF}" && "$(/usr/bin/stat -c %F -- "${SELF}")" == "regular file" ]] ||
  die "installer must be a regular non-symlink file"
[[ "$(/usr/bin/stat -c %u -- "${SELF}")" == 0 ]] || die "installer must be root-owned"
SELF_MODE="$(/usr/bin/stat -c %a -- "${SELF}")"
(( (8#${SELF_MODE} & 8#022) == 0 )) || die "installer must not be group/world writable"

for executable in \
  /usr/bin/awk /usr/bin/bash /usr/bin/cat /usr/bin/find /usr/bin/flock /usr/bin/gzip \
  /usr/bin/dirname /usr/bin/id /usr/bin/install /usr/bin/ln /usr/bin/mktemp /usr/bin/mv \
  /usr/bin/python3 /usr/bin/readlink /usr/bin/rm /usr/bin/sha256sum \
  /usr/bin/sort /usr/bin/stat /usr/bin/sync /usr/bin/chmod /usr/bin/chown \
  /usr/bin/xargs; do
  [[ -x "${executable}" ]] || die "missing required executable: ${executable}"
  [[ "$(/usr/bin/stat -c %u -- "${executable}")" == 0 ]] ||
    die "required executable is not root-owned: ${executable}"
done

/usr/bin/python3 -I -P - <<'PY' || exit 2
import platform
import sys

libc_name, libc_version = platform.libc_ver()
if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
    raise SystemExit("ERROR: privileged pilot release requires system CPython 3.12")
if platform.machine() != "x86_64" or libc_name != "glibc":
    raise SystemExit("ERROR: privileged pilot release requires x86_64 Linux with glibc")
if tuple(map(int, libc_version.split(".")[:2])) < (2, 28):
    raise SystemExit("ERROR: privileged pilot wheels require glibc >= 2.28")
PY

/usr/bin/install -d -o root -g root -m 0755 /opt/commu-protocol-pilots "${BASE}"
INSTALL_ROOT="$(/usr/bin/mktemp -d "${BASE}/.install.XXXXXX")"
COPIED_ARCHIVE="${INSTALL_ROOT}/bundle.tar.gz"
cleanup() {
  local status=$?
  trap - EXIT
  if [[ "${INSTALL_ROOT}" == "${BASE}"/.install.* && -d "${INSTALL_ROOT}" ]]; then
    /usr/bin/find "${INSTALL_ROOT}" -depth -delete
  fi
  exit "${status}"
}
trap cleanup EXIT

# Pin the source inode first, then copy through the open descriptor. Replacing
# the user's pathname after this point cannot change the bytes being installed.
exec 9<"${ARCHIVE}" || die "cannot open bundle"
[[ "$(/usr/bin/stat -Lc %F -- /proc/self/fd/9)" == "regular file" ]] ||
  die "bundle descriptor is not a regular file"
/usr/bin/cat <&9 >"${COPIED_ARCHIVE}"
exec 9<&-
/usr/bin/chmod 0600 "${COPIED_ARCHIVE}"
ACTUAL_ARCHIVE_SHA="$(/usr/bin/sha256sum -- "${COPIED_ARCHIVE}" | /usr/bin/awk '{print $1}')"
[[ "${ACTUAL_ARCHIVE_SHA}" == "${EXPECTED_ARCHIVE_SHA}" ]] || die "bundle SHA-256 mismatch"

# Extract only the independently reviewed repository code into a separate
# root-owned bootstrap tree. No program from the unprivileged bundle is run
# until the embedded manifest digest and every listed file digest agree.
VALIDATED_CODE_ROOT="${INSTALL_ROOT}/validated-code"
/usr/bin/install -d -o root -g root -m 0700 "${VALIDATED_CODE_ROOT}"
/usr/bin/python3 -I - \
  "${COPIED_ARCHIVE}" "${VALIDATED_CODE_ROOT}" \
  "${EXPECTED_REVIEWED_CODE_MANIFEST_SHA256}" <<'PY'
import hashlib
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
expected_manifest_sha = sys.argv[3]
manifest_name = "release/REVIEWED_CODE_FILES.sha256"
installer_names = {
    "release/repository/traffic_experiment/scripts/27_install_privileged_pilot_release.sh",
    "release/repository/traffic_experiment/scripts/30_install_privileged_matrix_release.sh",
}
members: dict[str, tarfile.TarInfo] = {}
total = 0
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle:
        name = member.name
        path = PurePosixPath(name)
        if (
            not name
            or name in members
            or name.startswith("/")
            or "\\" in name
            or not path.parts
            or path.parts[0] != "release"
            or any(part in ("", ".", "..") for part in path.parts)
            or not (member.isdir() or member.isreg())
            or member.size < 0
            or member.size > 20 * 1024**3
        ):
            raise SystemExit(f"unsafe archive member: {name!r}")
        total += member.size
        if total > 30 * 1024**3:
            raise SystemExit("archive expands beyond the 30-GiB safety limit")
        members[name] = member

    manifest_member = members.get(manifest_name)
    if manifest_member is None or not manifest_member.isreg() or manifest_member.size > 10 * 1024**2:
        raise SystemExit("archive has no safe reviewed-code manifest")
    source = bundle.extractfile(manifest_member)
    if source is None:
        raise SystemExit("cannot read reviewed-code manifest")
    manifest_bytes = source.read(10 * 1024**2 + 1)
    if len(manifest_bytes) > 10 * 1024**2:
        raise SystemExit("reviewed-code manifest is too large")
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha:
        raise SystemExit("release code is not the independently reviewed code set")
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("reviewed-code manifest is not UTF-8") from exc

    listed: dict[str, str] = {}
    for number, line in enumerate(manifest_text.splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (repository/[^\n]+)", line)
        if not match:
            raise SystemExit(f"malformed reviewed-code manifest line {number}")
        digest, relative = match.groups()
        archive_name = f"release/{relative}"
        path = PurePosixPath(archive_name)
        if (
            archive_name in installer_names
            or relative.startswith("repository/traffic_experiment/artifacts/")
            or relative.startswith("repository/traffic_experiment/.tools/")
            or archive_name in listed
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise SystemExit(f"unsafe reviewed-code path: {relative!r}")
        listed[archive_name] = digest

    actual = {
        name
        for name, member in members.items()
        if member.isreg()
        and name.startswith("release/repository/")
        and name not in installer_names
        and not name.startswith("release/repository/traffic_experiment/artifacts/")
        and not name.startswith("release/repository/traffic_experiment/.tools/")
    }
    if set(listed) != actual:
        raise SystemExit("reviewed-code manifest coverage mismatch")

    for archive_name, expected_digest in listed.items():
        member = members[archive_name]
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"cannot read reviewed file: {archive_name!r}")
        target = destination.joinpath(*PurePosixPath(archive_name.removeprefix("release/")).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        with source, os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_digest:
            raise SystemExit(f"reviewed file digest mismatch: {archive_name!r}")
PY

# Extract scope-defining data without running it, then validate it with the
# already authenticated parser before any general release extraction or
# scoped output/lock publication.
AUTHORIZED_DATA_ROOT="${INSTALL_ROOT}/authorized-data"
/usr/bin/install -d -o root -g root -m 0700 "${AUTHORIZED_DATA_ROOT}"
/usr/bin/python3 -I - "${COPIED_ARCHIVE}" "${AUTHORIZED_DATA_ROOT}" <<'PY'
import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
required = {
    "release/RELEASE_METADATA": 64 * 1024,
    "release/config/server.env": 1024 * 1024,
    "release/policy/service.state": 64 * 1024,
    "release/repository/traffic_experiment/artifacts/requests_32.jsonl": 1024**3,
    "release/repository/traffic_experiment/artifacts/event_summaries_10.jsonl": 1024**3,
}
found = {}
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle:
        if member.name not in required:
            continue
        if member.name in found or not member.isreg() or member.size > required[member.name]:
            raise SystemExit(f"unsafe authorized-data member: {member.name!r}")
        found[member.name] = member
    if set(found) != set(required):
        raise SystemExit("bundle is missing scope-defining data")
    for name, member in found.items():
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"cannot read scope-defining data: {name!r}")
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with source, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
PY

AUTHORIZED_RELEASE="${AUTHORIZED_DATA_ROOT}/release"
VALIDATOR="${VALIDATED_CODE_ROOT}/repository/traffic_experiment/scripts/privileged_pilot_config.py"
early_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "$2"
}
EARLY_METADATA="${AUTHORIZED_RELEASE}/RELEASE_METADATA"
EARLY_POLICY="${AUTHORIZED_RELEASE}/policy/service.state"
EARLY_CONFIG="${AUTHORIZED_RELEASE}/config/server.env"
[[ "$(early_value schema "${EARLY_METADATA}")" == commu-privileged-pilot-release-v1 &&
  "$(early_value purpose "${EARLY_METADATA}")" == protocol-pilots-only &&
  "$(early_value repository_sha "${EARLY_METADATA}")" == "${EXPECTED_REPOSITORY_SHA}" ]] ||
  die "bundle metadata is outside this installer's authorized release"
[[ "$(early_value schema "${EARLY_POLICY}")" == commu-privileged-pilot-policy-v2 &&
  "$(early_value repository_sha "${EARLY_POLICY}")" == "${EXPECTED_REPOSITORY_SHA}" &&
  "$(early_value service_user "${EARLY_POLICY}")" == "${EXPECTED_SERVICE_USER}" &&
  "$(early_value service_uid "${EARLY_POLICY}")" == "${EXPECTED_SERVICE_UID}" &&
  "$(early_value service_gid "${EARLY_POLICY}")" == "${EXPECTED_SERVICE_GID}" &&
  "$(early_value service_state_root "${EARLY_POLICY}")" == "${EXPECTED_SERVICE_STATE_ROOT}" ]] ||
  die "bundle policy is outside this installer's authorized service scope"
/usr/bin/python3 -I "${VALIDATOR}" check \
  --input "${EARLY_CONFIG}" --repository-sha "${EXPECTED_REPOSITORY_SHA}" \
  --release-root "${AUTHORIZED_RELEASE}" ||
  die "bundle config/artifacts failed authenticated scope validation"
early_config_value() {
  /usr/bin/python3 -I "${VALIDATOR}" get \
    --input "${EARLY_CONFIG}" --repository-sha "${EXPECTED_REPOSITORY_SHA}" --key "$1"
}
[[ "$(early_config_value VLLM_MODEL)" == "${EXPECTED_MODEL}" &&
  "$(early_config_value VLLM_SERVED_MODEL_NAME)" == "${EXPECTED_SERVED_MODEL}" &&
  "$(early_config_value VLLM_MODEL_REVISION)" == "${EXPECTED_MODEL_REVISION}" &&
  "$(early_config_value MANIFEST_PATH)" == "${EXPECTED_QA_MANIFEST}" &&
  "$(early_config_value MANIFEST_SHA256)" == "${EXPECTED_QA_SHA256}" &&
  "$(early_config_value SUMMARY_MANIFEST_PATH)" == "${EXPECTED_SUMMARY_MANIFEST}" &&
  "$(early_config_value SUMMARY_MANIFEST_SHA256)" == "${EXPECTED_SUMMARY_SHA256}" ]] ||
  die "bundle config is outside this installer's authorized pilot scope"

/usr/bin/install -d -o root -g root -m 0700 "${OUTPUT_BASE}"
/usr/bin/install -d -o root -g root -m 0755 /run/lock/commu-protocol-pilots
[[ -d /run/lock/commu-protocol-pilots && ! -L /run/lock/commu-protocol-pilots &&
  "$(/usr/bin/readlink -e -- /run/lock/commu-protocol-pilots)" == /run/lock/commu-protocol-pilots &&
  "$(/usr/bin/stat -c %u:%g:%a -- /run/lock/commu-protocol-pilots)" == 0:0:755 ]] ||
  die "shared-lock directory is not root-owned mode 0755"
VENV_PROBE="${INSTALL_ROOT}/venv-probe"
if ! /usr/bin/python3 -I -P -m venv --copies "${VENV_PROBE}" >/dev/null 2>&1 ||
  [[ ! -x "${VENV_PROBE}/bin/python" || ! -x "${VENV_PROBE}/bin/pip" ]]; then
  die "system Python venv support is missing; install python3.12-venv before retrying"
fi
if [[ -L "${VENV_PROBE}/lib64" && "$(/usr/bin/readlink -- "${VENV_PROBE}/lib64")" == lib ]]; then
  /usr/bin/rm -- "${VENV_PROBE}/lib64"
fi
[[ -z "$(/usr/bin/find "${VENV_PROBE}" -type l -print -quit)" ]] ||
  die "system Python --copies probe contains an unexpected symbolic link"
/usr/bin/find "${VENV_PROBE}" -depth -delete

EXTRACT_ROOT="${INSTALL_ROOT}/extract"
/usr/bin/install -d -o root -g root -m 0700 "${EXTRACT_ROOT}"
/usr/bin/python3 -I - "${COPIED_ARCHIVE}" "${EXTRACT_ROOT}" <<'PY'
import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
seen: set[str] = set()
members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
total = 0
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle:
        name = member.name
        path = PurePosixPath(name)
        if (
            not name
            or name in seen
            or name.startswith("/")
            or "\\" in name
            or not path.parts
            or path.parts[0] != "release"
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise SystemExit(f"unsafe or duplicate archive member: {name!r}")
        if not (member.isdir() or member.isreg()):
            raise SystemExit(f"archive links/devices are forbidden: {name!r}")
        if member.size < 0 or member.size > 20 * 1024**3:
            raise SystemExit(f"archive member has unsafe size: {name!r}")
        total += member.size
        if total > 30 * 1024**3:
            raise SystemExit("archive expands beyond the 30-GiB safety limit")
        seen.add(name)
        members.append((member, path))

    if "release/RELEASE_FILES.sha256" not in seen:
        raise SystemExit("archive has no release file manifest")
    for member, path in members:
        target = destination.joinpath(*path.parts)
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        source = bundle.extractfile(member)
        if source is None:
            os.close(descriptor)
            raise SystemExit(f"cannot read archive member: {member.name!r}")
        with source, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
PY

RELEASE="${EXTRACT_ROOT}/release"
[[ -d "${RELEASE}" && ! -L "${RELEASE}" ]] || die "archive has no safe release root"
/usr/bin/chown -R root:root "${RELEASE}"

REVIEWED_MANIFEST="${RELEASE}/REVIEWED_CODE_FILES.sha256"
[[ -f "${REVIEWED_MANIFEST}" && ! -L "${REVIEWED_MANIFEST}" ]] ||
  die "release has no reviewed-code manifest"
[[ "$(/usr/bin/sha256sum -- "${REVIEWED_MANIFEST}" | /usr/bin/awk '{print $1}')" == "${EXPECTED_REVIEWED_CODE_MANIFEST_SHA256}" ]] ||
  die "release code is not the independently reviewed code set"

/usr/bin/python3 -I - "${RELEASE}" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
reviewed = root / "REVIEWED_CODE_FILES.sha256"
reviewed_listed: set[str] = set()
for number, line in enumerate(reviewed.read_text(encoding="utf-8").splitlines(), 1):
    match = re.fullmatch(r"([0-9a-f]{64})  (repository/[^\n]+)", line)
    if not match:
        raise SystemExit(f"malformed reviewed-code manifest line {number}")
    relative = match.group(2)
    path = Path(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or relative in reviewed_listed
        or relative.startswith("repository/traffic_experiment/artifacts/")
        or relative in {
            "repository/traffic_experiment/scripts/27_install_privileged_pilot_release.sh",
            "repository/traffic_experiment/scripts/30_install_privileged_matrix_release.sh",
        }
    ):
        raise SystemExit(f"unsafe reviewed-code manifest path: {relative!r}")
    reviewed_listed.add(relative)
reviewed_actual = {
    str(path.relative_to(root)).replace("\\", "/")
    for path in (root / "repository").rglob("*")
    if path.is_file()
    and not str(path.relative_to(root)).replace("\\", "/").startswith(
        "repository/traffic_experiment/artifacts/"
    )
    and str(path.relative_to(root)).replace("\\", "/")
    not in {
        "repository/traffic_experiment/scripts/27_install_privileged_pilot_release.sh",
        "repository/traffic_experiment/scripts/30_install_privileged_matrix_release.sh",
    }
    and not str(path.relative_to(root)).replace("\\", "/").startswith(
        "repository/traffic_experiment/.tools/"
    )
}
if reviewed_listed != reviewed_actual:
    raise SystemExit("reviewed-code manifest coverage mismatch")

manifest = root / "RELEASE_FILES.sha256"
listed: set[str] = set()
for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
    match = re.fullmatch(r"([0-9a-f]{64})  (\./[^\n]+)", line)
    if not match:
        raise SystemExit(f"malformed release manifest line {number}")
    relative = match.group(2)[2:]
    if relative == "RELEASE_FILES.sha256" or relative in listed:
        raise SystemExit("release manifest is self-referential or duplicated")
    listed.add(relative)
actual = {
    str(path.relative_to(root)).replace("\\", "/")
    for path in root.rglob("*")
    if path.is_file() and path.name != "RELEASE_FILES.sha256"
}
if listed != actual:
    missing = sorted(actual - listed)
    extra = sorted(listed - actual)
    raise SystemExit(f"release manifest coverage mismatch missing={missing} extra={extra}")
PY
(
  cd "${RELEASE}"
  /usr/bin/sha256sum --check --strict --quiet REVIEWED_CODE_FILES.sha256
)
(
  cd "${RELEASE}"
  /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
)

metadata_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${RELEASE}/RELEASE_METADATA"
}
policy_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${RELEASE}/policy/service.state"
}
[[ "$(metadata_value schema)" == commu-privileged-pilot-release-v1 ]] || die "wrong release schema"
[[ "$(metadata_value purpose)" == protocol-pilots-only ]] || die "release is not pilots-only"
REPOSITORY_SHA="$(metadata_value repository_sha)" || die "release has no repository SHA"
[[ "${REPOSITORY_SHA}" == "${EXPECTED_REPOSITORY_SHA}" ]] || die "release repository SHA mismatch"
[[ "$(policy_value schema)" == commu-privileged-pilot-policy-v2 ]] || die "wrong policy schema"
[[ "$(policy_value repository_sha)" == "${REPOSITORY_SHA}" ]] || die "policy/release SHA mismatch"
SERVICE_UID="$(policy_value service_uid)" || die "policy has no service UID"
SERVICE_GID="$(policy_value service_gid)" || die "policy has no service GID"
SERVICE_USER="$(policy_value service_user)" || die "policy has no service user"
SERVICE_STATE_ROOT="$(policy_value service_state_root)" || die "policy has no service-state root"
[[ "${SERVICE_USER}" == "${EXPECTED_SERVICE_USER}" &&
  "${SERVICE_UID}" == "${EXPECTED_SERVICE_UID}" &&
  "${SERVICE_GID}" == "${EXPECTED_SERVICE_GID}" &&
  "${SERVICE_STATE_ROOT}" == "${EXPECTED_SERVICE_STATE_ROOT}" ]] ||
  die "bundle policy is outside the independently authorized service scope"
[[ "$(/usr/bin/id -u "${SERVICE_USER}")" == "${SERVICE_UID}" &&
  "$(/usr/bin/id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] ||
  die "policy service user/UID/GID do not match the system account"

VALIDATOR="${VALIDATED_CODE_ROOT}/repository/traffic_experiment/scripts/privileged_pilot_config.py"
CONFIG="${RELEASE}/config/server.env"
[[ -f "${VALIDATOR}" && ! -L "${VALIDATOR}" ]] || die "release config validator is missing"
/usr/bin/python3 -I "${VALIDATOR}" check \
  --input "${CONFIG}" --repository-sha "${REPOSITORY_SHA}" --release-root "${RELEASE}"
config_value() {
  /usr/bin/python3 -I "${VALIDATOR}" get \
    --input "${CONFIG}" --repository-sha "${REPOSITORY_SHA}" --key "$1"
}
[[ "$(config_value VLLM_MODEL)" == "${EXPECTED_MODEL}" &&
  "$(config_value VLLM_SERVED_MODEL_NAME)" == "${EXPECTED_SERVED_MODEL}" &&
  "$(config_value VLLM_MODEL_REVISION)" == "${EXPECTED_MODEL_REVISION}" &&
  "$(config_value MANIFEST_PATH)" == "${EXPECTED_QA_MANIFEST}" &&
  "$(config_value MANIFEST_SHA256)" == "${EXPECTED_QA_SHA256}" &&
  "$(config_value SUMMARY_MANIFEST_PATH)" == "${EXPECTED_SUMMARY_MANIFEST}" &&
  "$(config_value SUMMARY_MANIFEST_SHA256)" == "${EXPECTED_SUMMARY_SHA256}" ]] ||
  die "bundle config is outside the independently authorized pilot scope"

RUNTIME_DIR="${INSTALL_ROOT}/runner-venv"
RUNTIME_PYTHON="${RUNTIME_DIR}/bin/python"
CADDY="${RELEASE}/repository/traffic_experiment/.tools/caddy"
TRUSTED_WHEEL_MANIFEST="${VALIDATED_CODE_ROOT}/repository/traffic_experiment/privileged-pilot-wheels.cp312-linux-x86_64.sha256"
[[ -f "${CADDY}" && ! -L "${CADDY}" ]] || die "release Caddy is not a regular file"
[[ "$(/usr/bin/sha256sum -- "${CADDY}" | /usr/bin/awk '{print $1}')" == "${EXPECTED_CADDY_SHA256}" ]] ||
  die "Caddy does not match the independently pinned digest"

/usr/bin/python3 -I - "${RELEASE}/wheelhouse" "${TRUSTED_WHEEL_MANIFEST}" <<'PY'
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
        raise SystemExit(f"invalid trusted wheel manifest line {number}")
    expected[match.group(2)] = match.group(1)
entries = list(wheelhouse.iterdir())
actual = {path.name: path for path in entries}
if (
    set(actual) != set(expected)
    or any(path.is_symlink() or not path.is_file() for path in entries)
):
    raise SystemExit("wheelhouse filenames/coverage differ from trusted manifest")
for name, path in actual.items():
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
        raise SystemExit(f"wheel digest mismatch: {name}")
PY
TOOLS_DIR="${RELEASE}/repository/traffic_experiment/.tools"
[[ -d "${TOOLS_DIR}" && ! -L "${TOOLS_DIR}" &&
  "$(/usr/bin/find "${TOOLS_DIR}" -mindepth 1 -maxdepth 1 -printf '%f\n')" == caddy ]] ||
  die "release .tools must contain exactly the pinned Caddy binary"

# Never execute a copied user venv. Build a fresh root-owned runtime offline;
# pip's --require-hashes checks each selected distribution against the reviewed
# committed pilot-only lock while --no-index prevents network substitution.
/usr/bin/python3 -I -P -m venv --copies "${RUNTIME_DIR}" ||
  die "could not create root runner venv (python3.12-venv is required)"
HOME=/root PATH="${PATH}" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "${RUNTIME_PYTHON}" -I -P -m pip install \
    --no-index --only-binary=:all: --find-links "${RELEASE}/wheelhouse" --require-hashes \
    --no-deps \
    -r "${RELEASE}/repository/traffic_experiment/requirements-privileged-pilot.lock" ||
  die "offline hash-verified runner installation failed"
HOME=/root PATH="${PATH}" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "${RUNTIME_PYTHON}" -I -P -m pip check >/dev/null ||
  die "root runner dependency check failed"
if [[ -L "${RUNTIME_DIR}/lib64" && "$(/usr/bin/readlink -- "${RUNTIME_DIR}/lib64")" == lib ]]; then
  /usr/bin/rm -- "${RUNTIME_DIR}/lib64"
fi
[[ -z "$(/usr/bin/find "${RUNTIME_DIR}" -type l -print -quit)" ]] ||
  die "offline runner runtime unexpectedly contains a symbolic link"
/usr/bin/mv -- "${RUNTIME_DIR}" "${RELEASE}/repository/traffic_experiment/.venv-runner"
RUNTIME_PYTHON="${RELEASE}/repository/traffic_experiment/.venv-runner/bin/python"
RUNTIME_MANIFEST="${RELEASE}/INSTALLED_RUNTIME_FILES.sha256"
(
  cd "${RELEASE}"
  /usr/bin/find repository/traffic_experiment/.venv-runner -type f -print0 |
    LC_ALL=C /usr/bin/sort -z |
    /usr/bin/xargs -0 /usr/bin/sha256sum -- >"${RUNTIME_MANIFEST}"
  /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
)

# Make the verified release immutable to non-root users before executing its
# runtime for the dependency smoke check.
/usr/bin/find "${RELEASE}" -type d -exec chmod 0555 {} +
/usr/bin/find "${RELEASE}" -type f -exec chmod 0444 {} +
/usr/bin/chmod 0555 "${RUNTIME_PYTHON}"
/usr/bin/chmod 0555 "${CADDY}"
RUNNER="${RELEASE}/repository/traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh"
/usr/bin/chmod 0555 "${RUNNER}"
[[ -z "$(/usr/bin/find "${RELEASE}" \( ! -user root -o -perm /022 \) -print -quit)" ]] ||
  die "release ownership/mode hardening failed"

HOME=/root PATH="${PATH}" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "${RUNTIME_PYTHON}" -I -P -c \
  "import sys; sys.path.insert(0, '${RELEASE}/repository'); import aioquic, httpx, yaml; from traffic_experiment.traffic_measure import cli, pilot_evidence, worker_topology; from traffic_experiment.traffic_measure.http3_client import _runtime; _runtime()" ||
  die "root-owned runner runtime failed its import smoke test"
CADDY_VERSION="$(
  HOME=/root XDG_DATA_HOME="${INSTALL_ROOT}/caddy-data" \
    XDG_CONFIG_HOME="${INSTALL_ROOT}/caddy-config" "${CADDY}" version 2>&1
)" ||
  die "root-owned Caddy failed its smoke test"
[[ "${CADDY_VERSION}" == "${EXPECTED_CADDY_VERSION}" ]] || die "Caddy version output drifted"

DESTINATION="${BASE}/${REPOSITORY_SHA}"
[[ ! -e "${DESTINATION}" && ! -L "${DESTINATION}" ]] || die "release destination already exists"

# A root-owned parent makes this lock inode durable against the service user.
# The topology switcher and privileged pilot runner both flock the same inode.
GLOBAL_LOCK="/run/lock/commu-protocol-pilots/vllm-topology-${SERVICE_UID}.lock"
if [[ -e "${GLOBAL_LOCK}" || -L "${GLOBAL_LOCK}" ]]; then
  [[ -f "${GLOBAL_LOCK}" && ! -L "${GLOBAL_LOCK}" &&
    "$(/usr/bin/stat -c %u -- "${GLOBAL_LOCK}")" == 0 &&
    "$(/usr/bin/stat -c %g -- "${GLOBAL_LOCK}")" == "${SERVICE_GID}" &&
    "$(/usr/bin/stat -c %a -- "${GLOBAL_LOCK}")" == 660 &&
    "$(/usr/bin/stat -c %h -- "${GLOBAL_LOCK}")" == 1 ]] ||
    die "existing shared topology lock is unsafe"
  exec 8<>"${GLOBAL_LOCK}" || die "cannot open existing shared topology lock"
  /usr/bin/flock -n 8 || die "service topology is busy; release installation refused"
else
  LOCK_TMP="$(/usr/bin/mktemp /run/lock/commu-protocol-pilots/.vllm-topology.XXXXXX)"
  /usr/bin/chown root:"${SERVICE_GID}" "${LOCK_TMP}"
  /usr/bin/chmod 0660 "${LOCK_TMP}"
  exec 8<>"${LOCK_TMP}" || die "cannot open new shared topology lock"
  /usr/bin/flock -n 8 || die "cannot lock new shared topology inode"
  if ! /usr/bin/ln "${LOCK_TMP}" "${GLOBAL_LOCK}" 2>/dev/null; then
    /usr/bin/rm -f -- "${LOCK_TMP}"
    die "could not atomically publish the shared topology lock"
  fi
  /usr/bin/rm -f -- "${LOCK_TMP}"
fi
/usr/bin/install -d -o root -g root -m 0700 \
  "${OUTPUT_BASE}/${REPOSITORY_SHA}" \
  "${OUTPUT_BASE}/${REPOSITORY_SHA}/snapshots"
/usr/bin/mv -- "${RELEASE}" "${DESTINATION}"
/usr/bin/sync -f "${DESTINATION}"
/usr/bin/sync -f "${BASE}"
/usr/bin/sync -f "${OUTPUT_BASE}/${REPOSITORY_SHA}"

RUNNER="${DESTINATION}/repository/traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh"
[[ -f "${RUNNER}" ]] || die "release runner is missing"
trap - EXIT
/usr/bin/find "${INSTALL_ROOT}" -depth -delete
printf 'PRIVILEGED_PILOT_RELEASE_INSTALLED repository_sha=%s\n' "${REPOSITORY_SHA}"
printf 'release_root=%s\n' "${DESTINATION}"
printf 'run_command=sudo %s run --service-state /absolute/path/to/service.state\n' "${RUNNER}"
