#!/usr/bin/bash -p
set -euo pipefail
set +x
umask 077

# Run only the one-request TLS 1.3 and HTTP/3 protocol pilots from an installed,
# root-owned release. No action in this launcher can start the measured matrix.

EXPECTED_CONTROLLER_CWD=/home/wongshingyin
EXPECTED_CONTROLLER_EXE=/usr/bin/bash
EXPECTED_CONTROLLER_LAUNCHER=/home/wongshingyin/commu/traffic_experiment/scripts/03_start_vllm_dual.sh
EXPECTED_API_PYTHON=/home/wongshingyin/.venvs/commu-qwen35-e12240f/bin/python
EXPECTED_API_VLLM=/home/wongshingyin/.venvs/commu-qwen35-e12240f/bin/vllm
EXPECTED_API_LD_LIBRARY_PATH=/home/wongshingyin/.venvs/commu-qwen35-e12240f/lib/python3.12/site-packages/nvidia/cu13/lib:/home/wongshingyin/.venvs/commu-qwen35-e12240f/lib/python3.12/site-packages/torch/lib
EXPECTED_SERVICE_USER=wongshingyin
EXPECTED_SERVICE_UID=1007
EXPECTED_SERVICE_GID=1007
EXPECTED_SERVICE_STATE=/home/wongshingyin/.config/commu/qwen35-e12240f/service-attempt-5-gpu2-1.state
EXPECTED_GPU_UUID_PIN=GPU-41d1f86d-0197-51fe-c1ef-ad53c99e3223

FIXED_PATH=/usr/sbin:/usr/bin
if [[ "${COMMU_PRIVILEGED_PILOT_CLEAN_ENV:-}" != 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'ERROR: runner must run as root\n' >&2; exit 2; }
  SELF="$(/usr/bin/readlink -e -- "$0")" || exit 2
  [[ -f "${SELF}" && ! -L "${SELF}" && "$(/usr/bin/stat -c %u -- "${SELF}")" == 0 ]] || exit 2
  exec /usr/bin/env -i \
    COMMU_PRIVILEGED_PILOT_CLEAN_ENV=1 \
    HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
    /usr/bin/bash -p "${SELF}" "$@"
fi
PATH="${FIXED_PATH}"
export PATH HOME LANG LC_ALL TZ PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE PYTHONSAFEPATH
cd /

# The sentinel is not a trust decision. Even if a caller spells it manually,
# no ambient variable outside this harmless allowlist may survive.
while IFS='=' read -r inherited_name _; do
  case "${inherited_name}" in
    COMMU_PRIVILEGED_PILOT_CLEAN_ENV|HOME|LANG|LC_ALL|TZ|PATH|PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|PYTHONSAFEPATH|PWD|SHLVL|_) ;;
    *) printf 'ERROR: unsanitized environment variable: %s\n' "${inherited_name}" >&2; exit 2 ;;
  esac
done < <(/usr/bin/env)
[[ "${EUID}" -eq 0 ]] || { printf 'ERROR: runner must run as root\n' >&2; exit 2; }
[[ "${HOME}" == /root && "${LANG}" == C.UTF-8 && "${LC_ALL}" == C.UTF-8 &&
  "${TZ}" == UTC && "${PATH}" == "${FIXED_PATH}" &&
  "${PYTHONNOUSERSITE}" == 1 && "${PYTHONDONTWRITEBYTECODE}" == 1 &&
  "${PYTHONSAFEPATH}" == 1 && "${PWD}" == / ]] || {
  printf 'ERROR: sanitized environment values do not match the fixed policy\n' >&2
  exit 2
}

ACTION="${1:-check}"
[[ $# -le 1 ]] || { printf 'usage: %s {check|run|admission}\n' "$0" >&2; exit 2; }
case "${ACTION}" in check|run|admission) ;; *) printf 'usage: %s {check|run|admission}\n' "$0" >&2; exit 2 ;; esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
RELEASE_ROOT="$(cd -- "${REPOSITORY_ROOT}/.." && pwd)"
CONFIG="${RELEASE_ROOT}/config/server.env"
POLICY="${RELEASE_ROOT}/policy/service.state"
METADATA="${RELEASE_ROOT}/RELEASE_METADATA"
MANIFEST="${RELEASE_ROOT}/RELEASE_FILES.sha256"
RUNTIME_MANIFEST="${RELEASE_ROOT}/INSTALLED_RUNTIME_FILES.sha256"
VALIDATOR="${SCRIPT_DIR}/privileged_pilot_config.py"
PILOT_SCRIPT="${SCRIPT_DIR}/22_validate_protocol_pilots.sh"
RUNNER_PYTHON="${EXPERIMENT_ROOT}/.venv-runner/bin/python"
CADDY="${EXPERIMENT_ROOT}/.tools/caddy"
LOCK_HELD=0
LOCK_ID=""
SERVICE_LOCK_DIR=""
GLOBAL_LOCK_FILE=""
ADMISSION_CANDIDATE=""
SERVICE_STATE_ORIGINAL=""
SERVICE_STATE_SNAPSHOT=""
SERVICE_STATE_SOURCE_ID=""
ACTIVE_CONFIG_ORIGINAL=""
ACTIVE_CONFIG_SNAPSHOT=""
ACTIVE_CONFIG_SOURCE_ID=""
ADMISSION_PUBLISHED=0
ADMISSION_MARKER_ID=""

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

regular_root_file() {
  local path="$1" mode
  [[ -f "${path}" && ! -L "${path}" && "$(/usr/bin/stat -c %F -- "${path}")" == "regular file" ]] || return 1
  [[ "$(/usr/bin/stat -c %u -- "${path}")" == 0 && "$(/usr/bin/stat -c %h -- "${path}")" == 1 ]] || return 1
  mode="$(/usr/bin/stat -c %a -- "${path}")"
  (( (8#${mode} & 8#022) == 0 ))
}

trusted_root_directory() {
  local path="$1" expected_mode="${2:-}" mode
  [[ -d "${path}" && ! -L "${path}" &&
    "$(/usr/bin/readlink -e -- "${path}")" == "${path}" &&
    "$(/usr/bin/stat -c %u:%g -- "${path}")" == 0:0 ]] || return 1
  mode="$(/usr/bin/stat -c %a -- "${path}")"
  if [[ -n "${expected_mode}" ]]; then
    [[ "${mode}" == "${expected_mode}" ]]
  else
    (( (8#${mode} & 8#022) == 0 ))
  fi
}

root_command() {
  local name="$1" path resolved mode
  path="$(command -v "${name}" 2>/dev/null)" || return 1
  case "${path}" in /usr/bin/*|/usr/sbin/*) ;; *) return 1 ;; esac
  resolved="$(/usr/bin/readlink -e -- "${path}")" || return 1
  [[ -f "${resolved}" && "$(/usr/bin/stat -c %u -- "${resolved}")" == 0 ]] || return 1
  mode="$(/usr/bin/stat -c %a -- "${resolved}")"
  (( (8#${mode} & 8#022) == 0 )) || return 1
  printf '%s\n' "${path}"
}

metadata_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${METADATA}"
}
policy_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${POLICY}"
}
state_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${SERVICE_STATE}"
}
candidate_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "${ADMISSION_CANDIDATE}"
}
config_value() {
  /usr/bin/python3 -I "${VALIDATOR}" get \
    --input "$1" --repository-sha "${REPOSITORY_SHA}" --key "$2"
}
sha256_file() { /usr/bin/sha256sum -- "$1" | /usr/bin/awk '{print $1}'; }

snapshot_user_file() {
  /usr/bin/python3 -I - "$1" "$2" "$3" "$4" "$5" <<'PY'
import hashlib
import os
import stat
import sys

source, destination = sys.argv[1:3]
expected_uid, expected_gid, expected_mode = map(int, sys.argv[3:])
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
source_fd = os.open(source, flags)
try:
    before = os.fstat(source_fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) != expected_mode
        or before.st_nlink != 1
        or before.st_size > 1024 * 1024
    ):
        raise SystemExit("source metadata is outside policy")
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        output = os.fdopen(destination_fd, "wb", closefd=False)
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                raise SystemExit("source grew beyond limit")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(destination_fd)
    finally:
        output.close()
        os.close(destination_fd)
    after = os.fstat(source_fd)
    path_stat = os.stat(source, follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (after.st_dev, after.st_ino) != (path_stat.st_dev, path_stat.st_ino)
    ):
        raise SystemExit("source identity changed during snapshot")
    print(f"{after.st_dev}:{after.st_ino}:{digest.hexdigest()}")
finally:
    os.close(source_fd)
PY
}

verify_user_file_identity() {
  /usr/bin/python3 -I - "$1" "$2" "$3" "$4" "$5" <<'PY'
import hashlib
import os
import stat
import sys

source, expected = sys.argv[1:3]
expected_uid, expected_gid, expected_mode = map(int, sys.argv[3:])
expected_dev, expected_ino, expected_digest = expected.split(":", 2)
fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
try:
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_nlink != 1
        or str(metadata.st_dev) != expected_dev
        or str(metadata.st_ino) != expected_ino
    ):
        raise SystemExit(1)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 64 * 1024):
        digest.update(chunk)
    if digest.hexdigest() != expected_digest:
        raise SystemExit(1)
    path_stat = os.stat(source, follow_symlinks=False)
    if (path_stat.st_dev, path_stat.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise SystemExit(1)
finally:
    os.close(fd)
PY
}

process_stat_tail() {
  local pid="$1" line
  [[ "${pid}" =~ ^[0-9]+$ && -r "/proc/${pid}/stat" ]] || return 1
  IFS= read -r line <"/proc/${pid}/stat" || return 1
  [[ "${line}" == *") "* ]] || return 1
  printf '%s\n' "${line##*) }"
}
process_field() { local tail; tail="$(process_stat_tail "$1")" || return 1; /usr/bin/awk -v n="$2" '{print $n}' <<<"${tail}"; }
process_state() { process_field "$1" 1; }
process_parent() { process_field "$1" 2; }
process_group() { process_field "$1" 3; }
process_session() { process_field "$1" 4; }
process_ticks() { process_field "$1" 20; }
process_uid() { /usr/bin/awk '/^Uid:/ {print $2; exit}' "/proc/$1/status" 2>/dev/null; }
process_exe() { /usr/bin/readlink -e -- "/proc/$1/exe"; }
process_args() {
  local pid="$1"
  local -n result="$2"
  local argument
  result=()
  while IFS= read -r -d '' argument; do result+=("${argument}"); done <"/proc/${pid}/cmdline"
}
arrays_equal() {
  local -n left="$1" right="$2"
  local index
  [[ "${#left[@]}" -eq "${#right[@]}" ]] || return 1
  for index in "${!left[@]}"; do [[ "${left[index]}" == "${right[index]}" ]] || return 1; done
}

is_descendant() {
  local pid="$1" ancestor="$2" parent steps=0
  while [[ "${pid}" =~ ^[0-9]+$ && "${pid}" -gt 1 && "${steps}" -lt 128 ]]; do
    [[ "${pid}" == "${ancestor}" ]] && return 0
    parent="$(process_parent "${pid}" 2>/dev/null || true)"
    [[ "${parent}" =~ ^[0-9]+$ && "${parent}" != "${pid}" ]] || return 1
    pid="${parent}"
    ((steps+=1))
  done
  return 1
}

process_env_value() {
  local pid="$1" key="$2" entry value="" count=0
  [[ "${pid}" =~ ^[0-9]+$ && -r "/proc/${pid}/environ" ]] || return 1
  while IFS= read -r -d '' entry; do
    case "${entry}" in
      "${key}="*) value="${entry#*=}"; ((count+=1)) ;;
    esac
  done <"/proc/${pid}/environ"
  [[ "${count}" -eq 1 && -n "${value}" && "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || return 1
  printf '%s' "${value}"
}

ss_rows() {
  local output
  output="$(/usr/bin/ss "$@" 2>/dev/null)" || return 1
  printf '%s' "${output}"
}
port_closed() { local rows; rows="$(ss_rows -H -ltn "sport = :$1")" || return 1; [[ -z "${rows}" ]]; }
listener_pid() {
  local port="$1" rows pids
  rows="$(ss_rows -H -ltnp "sport = :${port}")" || return 1
  [[ -n "${rows}" ]] || return 1
  /usr/bin/grep -Eq "127\\.0\\.0\\.1:${port}[[:space:]]" <<<"${rows}" || return 1
  pids="$(/usr/bin/grep -oE 'pid=[0-9]+' <<<"${rows}" | /usr/bin/cut -d= -f2 | /usr/bin/sort -u)"
  [[ "$(/usr/bin/wc -w <<<"${pids}")" -eq 1 ]] || return 1
  printf '%s\n' "${pids}"
}

release_precheck() {
  local command_name directory
  for directory in / /home /opt /opt/commu-protocol-pilots \
    /opt/commu-protocol-pilots/releases /usr /usr/bin /usr/sbin \
    /var /var/lib /run; do
    trusted_root_directory "${directory}" || die "unsafe privileged path component: ${directory}"
  done
  trusted_root_directory /run/lock 1777 || die "unsafe system lock directory"
  trusted_root_directory /run/lock/commu-protocol-pilots 755 ||
    die "unsafe project lock directory"
  for file in "${CONFIG}" "${POLICY}" "${METADATA}" "${MANIFEST}" "${RUNTIME_MANIFEST}" "${VALIDATOR}" "${PILOT_SCRIPT}" "${RUNNER_PYTHON}" "${CADDY}"; do
    regular_root_file "${file}" || die "unsafe release file: ${file}"
  done
  case "${RELEASE_ROOT}" in /opt/commu-protocol-pilots/releases/[0-9a-f][0-9a-f]*) ;; *) die "release is outside the fixed installation root" ;; esac
  for command_name in awk bash chmod curl cut date dumpcap ethtool find flock grep hostname ip iperf3 mkdir nvidia-smi python3 readlink rm sed sha256sum sleep sort ss stat tail tc tee tshark uname wc; do
    root_command "${command_name}" >/dev/null || die "unsafe or missing system command: ${command_name}"
  done
  (
    cd "${RELEASE_ROOT}"
    /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
    /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
  ) || die "release file digest verification failed"
  [[ "$(metadata_value schema)" == commu-privileged-pilot-release-v1 ]] || die "wrong release schema"
  [[ "$(metadata_value purpose)" == protocol-pilots-only ]] || die "release is not pilots-only"
  REPOSITORY_SHA="$(metadata_value repository_sha)" || die "missing repository SHA"
  [[ "${REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "invalid repository SHA"
  [[ "$(basename -- "${RELEASE_ROOT}")" == "${REPOSITORY_SHA}" ]] || die "release directory/SHA mismatch"
  for directory in "${RELEASE_ROOT}" "${RELEASE_ROOT}/repository" \
    "${EXPERIMENT_ROOT}" "${SCRIPT_DIR}"; do
    trusted_root_directory "${directory}" || die "unsafe release directory: ${directory}"
  done
  /usr/bin/python3 -I "${VALIDATOR}" check \
    --input "${CONFIG}" --repository-sha "${REPOSITORY_SHA}" --release-root "${RELEASE_ROOT}" ||
    die "privileged config validation failed"
}

load_policy() {
  [[ "$(policy_value schema)" == commu-privileged-pilot-policy-v1 ]] || die "wrong policy schema"
  [[ "$(policy_value repository_sha)" == "${REPOSITORY_SHA}" ]] || die "policy/release SHA mismatch"
  SERVICE_STATE="$(policy_value service_state)" || die "missing service state path"
  SERVICE_USER="$(policy_value service_user)" || die "missing service user"
  SERVICE_UID="$(policy_value service_uid)" || die "missing service UID"
  SERVICE_GID="$(policy_value service_gid)" || die "missing service GID"
  EXPECTED_GPU_UUID="$(policy_value expected_gpu_uuid)" || die "missing expected GPU UUID"
  [[ "${SERVICE_USER}" == "${EXPECTED_SERVICE_USER}" &&
    "${SERVICE_UID}" == "${EXPECTED_SERVICE_UID}" &&
    "${SERVICE_GID}" == "${EXPECTED_SERVICE_GID}" &&
    "${SERVICE_STATE}" == "${EXPECTED_SERVICE_STATE}" &&
    "${EXPECTED_GPU_UUID}" == "${EXPECTED_GPU_UUID_PIN}" ]] ||
    die "installed service policy is outside the reviewed scope"
  [[ "${SERVICE_STATE}" = /* && "${SERVICE_STATE}" != *$'\n'* ]] || die "unsafe service state path"
  [[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*$ && "${SERVICE_UID}" =~ ^[1-9][0-9]*$ && "${SERVICE_GID}" =~ ^[1-9][0-9]*$ ]] || die "unsafe service identity policy"
  [[ "${EXPECTED_GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ ]] || die "unsafe GPU UUID policy"
  [[ "$(/usr/bin/id -u "${SERVICE_USER}")" == "${SERVICE_UID}" && "$(/usr/bin/id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] || die "service account identity drifted"
}

check_output_hierarchy() {
  local root="/var/lib/commu-protocol-pilots/${REPOSITORY_SHA}" path
  for path in /var/lib/commu-protocol-pilots "${root}" "${root}/runs" "${root}/caddy"; do
    [[ -d "${path}" && ! -L "${path}" && "$(/usr/bin/stat -c %u -- "${path}")" == 0 && "$(/usr/bin/stat -c %g -- "${path}")" == 0 && "$(/usr/bin/stat -c %a -- "${path}")" == 700 ]] ||
      die "unsafe root-owned output directory: ${path}"
  done
  OUTPUT_ROOT="${root}"
  PROTOCOL_ROOT="${root}/runs/protocol_validation"
  NETWORK_STATE_ROOT="${root}/network_state"
  if [[ -e "${NETWORK_STATE_ROOT}" || -L "${NETWORK_STATE_ROOT}" ]]; then
    [[ -d "${NETWORK_STATE_ROOT}" && ! -L "${NETWORK_STATE_ROOT}" && "$(/usr/bin/stat -c %u -- "${NETWORK_STATE_ROOT}")" == 0 ]] || die "unsafe network-state directory"
  else
    /usr/bin/install -d -o root -g root -m 0700 "${NETWORK_STATE_ROOT}"
  fi
}

acquire_service_lock() {
  local parent
  parent="$(dirname -- "${SERVICE_STATE}")"
  [[ -d "${parent}" && ! -L "${parent}" && "$(/usr/bin/stat -c %u -- "${parent}")" == "${SERVICE_UID}" ]] || die "unsafe service-state parent"
  GLOBAL_LOCK_FILE="/run/lock/commu-protocol-pilots/vllm-topology-${SERVICE_UID}.lock"
  [[ -f "${GLOBAL_LOCK_FILE}" && ! -L "${GLOBAL_LOCK_FILE}" &&
    "$(/usr/bin/stat -c %u -- "${GLOBAL_LOCK_FILE}")" == 0 &&
    "$(/usr/bin/stat -c %g -- "${GLOBAL_LOCK_FILE}")" == "${SERVICE_GID}" &&
    "$(/usr/bin/stat -c %a -- "${GLOBAL_LOCK_FILE}")" == 660 &&
    "$(/usr/bin/stat -c %h -- "${GLOBAL_LOCK_FILE}")" == 1 ]] ||
    die "unsafe or missing shared topology lock"
  exec 8<>"${GLOBAL_LOCK_FILE}" || die "cannot open shared topology lock"
  /usr/bin/flock -n 8 || die "service topology is busy; shared lock not acquired"
  SERVICE_LOCK_DIR="${SERVICE_STATE}.lock.d"
  /usr/bin/mkdir --mode=0700 -- "${SERVICE_LOCK_DIR}" 2>/dev/null || die "service topology is busy; lock not acquired"
  LOCK_ID="$(/usr/bin/stat -c %d:%i -- "${SERVICE_LOCK_DIR}")" || die "cannot record service lock identity"
  LOCK_HELD=1
  [[ -d "${SERVICE_LOCK_DIR}" && ! -L "${SERVICE_LOCK_DIR}" && "$(/usr/bin/stat -c %u -- "${SERVICE_LOCK_DIR}")" == 0 ]] || die "unsafe service lock"
}

pin_service_state() {
  SERVICE_STATE_ORIGINAL="${SERVICE_STATE}"
  SERVICE_STATE_SNAPSHOT="${OUTPUT_ROOT}/.service-state-$$"
  [[ ! -e "${SERVICE_STATE_SNAPSHOT}" && ! -L "${SERVICE_STATE_SNAPSHOT}" ]] ||
    die "service-state snapshot path already exists"
  SERVICE_STATE_SOURCE_ID="$(snapshot_user_file \
    "${SERVICE_STATE_ORIGINAL}" "${SERVICE_STATE_SNAPSHOT}" \
    "${SERVICE_UID}" "${SERVICE_GID}" 384)" ||
    die "could not securely snapshot service state"
  regular_root_file "${SERVICE_STATE_SNAPSHOT}" || die "unsafe service-state snapshot"
  SERVICE_STATE="${SERVICE_STATE_SNAPSHOT}"
}

release_service_lock() {
  [[ "${LOCK_HELD}" -eq 1 ]] || return 0
  if [[ -d "${SERVICE_LOCK_DIR}" && ! -L "${SERVICE_LOCK_DIR}" && "$(/usr/bin/stat -c %u -- "${SERVICE_LOCK_DIR}")" == 0 && "$(/usr/bin/stat -c %d:%i -- "${SERVICE_LOCK_DIR}")" == "${LOCK_ID}" ]]; then
    /usr/bin/rmdir -- "${SERVICE_LOCK_DIR}" || return 1
    LOCK_HELD=0
    return 0
  fi
  printf 'Service lock identity changed; refusing cleanup: %s\n' "${SERVICE_LOCK_DIR}" >&2
  return 1
}
cleanup() {
  local status=$?
  trap - EXIT
  if [[ "${status}" -ne 0 && "${ADMISSION_PUBLISHED}" -eq 1 &&
    -f "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" &&
    ! -L "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" &&
    "$(/usr/bin/stat -c %u:%h:%d:%i -- "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK")" == "0:1:${ADMISSION_MARKER_ID}" ]]; then
    /usr/bin/rm -f -- "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" || true
  fi
  if [[ -n "${ADMISSION_CANDIDATE}" && -f "${ADMISSION_CANDIDATE}" &&
    ! -L "${ADMISSION_CANDIDATE}" &&
    "$(/usr/bin/stat -c %u -- "${ADMISSION_CANDIDATE}")" == 0 &&
    "$(/usr/bin/stat -c %h -- "${ADMISSION_CANDIDATE}")" == 1 ]]; then
    rm -f -- "${ADMISSION_CANDIDATE}" || [[ "${status}" -ne 0 ]] || status=1
  fi
  for snapshot in "${ACTIVE_CONFIG_SNAPSHOT}" "${SERVICE_STATE_SNAPSHOT}"; do
    [[ -n "${snapshot}" ]] || continue
    case "${snapshot}" in
      "${OUTPUT_ROOT}"/.active-config-[0-9]*|"${OUTPUT_ROOT}"/.service-state-[0-9]*) ;;
      *) continue ;;
    esac
    if [[ -f "${snapshot}" && ! -L "${snapshot}" &&
      "$(/usr/bin/stat -c %u:%h -- "${snapshot}")" == 0:1 ]]; then
      /usr/bin/rm -f -- "${snapshot}" || true
    fi
  done
  if ! release_service_lock; then
    [[ "${status}" -ne 0 ]] || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM HUP

verify_active_service() {
  local state_parent active_config active_hash controller api engine gpu_index gpu_uuid
  local active_vllm active_ld controller_cwd api_cwd engine_cwd
  local -a controller_args=() api_args=() engine_args=() expected_api_args=()
  state_parent="$(dirname -- "${SERVICE_STATE_ORIGINAL}")"
  regular_root_file "${SERVICE_STATE}" || die "unsafe pinned service state"
  [[ "$(state_value schema)" == commu-vllm-service-state-v2 && "$(state_value status)" == running ]] || die "service state is not a running v2 record"
  [[ "$(state_value repository_sha)" == "${REPOSITORY_SHA}" ]] || die "service/release repository SHA mismatch"
  [[ "$(state_value worker_count)" == 1 ]] || die "service is not in one-worker mode"
  [[ "$(state_value worker_0_port)" == 8000 ]] || die "service port drifted"
  ! /usr/bin/grep -q '^worker_1_' "${SERVICE_STATE}" || die "unexpected second worker in service state"

  ACTIVE_CONFIG_ORIGINAL="$(state_value config)" || die "service state has no config"
  [[ "${ACTIVE_CONFIG_ORIGINAL}" = /* && "${ACTIVE_CONFIG_ORIGINAL}" != *$'\n'* ]] ||
    die "active config path is unsafe"
  if [[ -z "${ACTIVE_CONFIG_SNAPSHOT}" ]]; then
    ACTIVE_CONFIG_SNAPSHOT="${OUTPUT_ROOT}/.active-config-$$"
    [[ ! -e "${ACTIVE_CONFIG_SNAPSHOT}" && ! -L "${ACTIVE_CONFIG_SNAPSHOT}" ]] ||
      die "active-config snapshot path already exists"
    ACTIVE_CONFIG_SOURCE_ID="$(snapshot_user_file \
      "${ACTIVE_CONFIG_ORIGINAL}" "${ACTIVE_CONFIG_SNAPSHOT}" \
      "${SERVICE_UID}" "${SERVICE_GID}" 256)" ||
      die "could not securely snapshot active config"
  fi
  active_config="${ACTIVE_CONFIG_SNAPSHOT}"
  regular_root_file "${active_config}" || die "unsafe active-config snapshot"
  active_hash="$(state_value config_sha256)" || die "state has no config digest"
  [[ "${active_hash}" =~ ^[0-9a-f]{64}$ &&
    "$(sha256_file "${active_config}")" == "${active_hash}" ]] || die "active config digest drifted"
  /usr/bin/python3 -I "${VALIDATOR}" check-active \
    --input "${active_config}" --repository-sha "${REPOSITORY_SHA}" ||
    die "active service config is not strict inert data"
  for field in PARALLEL_WORKERS CUDA_VISIBLE_DEVICES VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT VLLM_PORT_STEP VLLM_MODEL VLLM_SERVED_MODEL_NAME VLLM_MODEL_REVISION TENSOR_PARALLEL_SIZE MAX_MODEL_LEN GPU_MEMORY_UTILIZATION; do
    [[ "$(config_value "${active_config}" "${field}")" == "$(config_value "${CONFIG}" "${field}")" ]] || die "active service differs from pilot release: ${field}"
  done
  active_vllm="$(config_value "${active_config}" VLLM_BIN)"
  active_ld="$(config_value "${active_config}" LD_LIBRARY_PATH)"
  [[ "${active_vllm}" == "${EXPECTED_API_VLLM}" &&
    "${active_ld}" == "${EXPECTED_API_LD_LIBRARY_PATH}" ]] ||
    die "active service executable/library path is outside policy"

  gpu_index="$(config_value "${CONFIG}" CUDA_VISIBLE_DEVICES)"
  gpu_uuid="$(/usr/bin/nvidia-smi -i "${gpu_index}" --query-gpu=uuid --format=csv,noheader,nounits 2>/dev/null)" || die "GPU inventory failed"
  gpu_uuid="${gpu_uuid//[[:space:]]/}"
  [[ "${gpu_uuid}" == "${EXPECTED_GPU_UUID}" && "$(state_value worker_0_gpu_index)" == "${gpu_index}" && "$(state_value worker_0_gpu_uuid)" == "${gpu_uuid}" ]] || die "GPU identity drifted"

  controller="$(state_value controller_pid)"; CONTROLLER_TICKS="$(state_value controller_start_ticks)"
  [[ "$(state_value controller_uid)" == "${SERVICE_UID}" && "$(process_uid "${controller}")" == "${SERVICE_UID}" && "$(process_ticks "${controller}")" == "${CONTROLLER_TICKS}" && "$(process_state "${controller}")" != Z ]] || die "controller identity drifted"
  [[ "$(process_group "${controller}")" == "$(state_value controller_pgid)" && "$(process_session "${controller}")" == "$(state_value controller_sid)" ]] || die "controller process group/session drifted"
  controller_cwd="$(/usr/bin/readlink -e -- "/proc/${controller}/cwd")" || die "cannot inspect controller cwd"
  process_args "${controller}" controller_args || die "cannot inspect controller argv"
  [[ "${controller_cwd}" == "${EXPECTED_CONTROLLER_CWD}" &&
    "$(process_exe "${controller}")" == "${EXPECTED_CONTROLLER_EXE}" &&
    "${#controller_args[@]}" -eq 2 && "${controller_args[0]}" == bash &&
    "${controller_args[1]}" == "${EXPECTED_CONTROLLER_LAUNCHER}" ]] ||
    die "controller cwd/argv drifted"
  [[ "$(/usr/bin/readlink -e -- "$(process_env_value "${controller}" EXPERIMENT_ENV_FILE)")" == "${ACTIVE_CONFIG_ORIGINAL}" ]] || die "controller config environment drifted"

  api="$(listener_pid 8000)" || die "cannot identify the loopback vLLM listener"
  [[ "${api}" == "$(state_value worker_0_api_pid)" && "$(process_ticks "${api}")" == "$(state_value worker_0_api_start_ticks)" && "$(process_uid "${api}")" == "${SERVICE_UID}" ]] || die "API process identity drifted"
  [[ "$(process_state "${api}")" != Z && "$(process_group "${api}")" == "${api}" &&
    "$(process_session "${api}")" == "${api}" ]] || die "API process group/session drifted"
  is_descendant "${api}" "${controller}" || die "API listener is not owned by the controller"
  api_cwd="$(/usr/bin/readlink -e -- "/proc/${api}/cwd")" || die "cannot inspect API cwd"
  process_args "${api}" api_args || die "cannot inspect API argv"
  expected_api_args=(
    "${EXPECTED_API_PYTHON}" "${EXPECTED_API_VLLM}" serve
    "$(config_value "${active_config}" VLLM_MODEL)"
    --host "$(config_value "${active_config}" VLLM_HOST)"
    --port "$(config_value "${active_config}" VLLM_PORT)"
    --served-model-name "$(config_value "${active_config}" VLLM_SERVED_MODEL_NAME)"
    --tensor-parallel-size "$(config_value "${active_config}" TENSOR_PARALLEL_SIZE)"
    --max-model-len "$(config_value "${active_config}" MAX_MODEL_LEN)"
    --gpu-memory-utilization "$(config_value "${active_config}" GPU_MEMORY_UTILIZATION)"
    --language-model-only
    --revision "$(config_value "${active_config}" VLLM_MODEL_REVISION)"
    --generation-config vllm
  )
  arrays_equal api_args expected_api_args || die "API argv drifted"
  [[ "${api_cwd}" == "${EXPECTED_CONTROLLER_CWD}" &&
    "$(process_exe "${api}")" == "$(/usr/bin/readlink -e -- "${EXPECTED_API_PYTHON}")" &&
    "$(process_env_value "${api}" CUDA_VISIBLE_DEVICES)" == "${gpu_index}" &&
    "$(process_env_value "${api}" LD_LIBRARY_PATH)" == "${active_ld}" &&
    "$(/usr/bin/readlink -e -- "$(process_env_value "${api}" EXPERIMENT_ENV_FILE)")" == "${ACTIVE_CONFIG_ORIGINAL}" ]] || die "API cwd/argv/environment drifted"
  port_closed 8001 || die "secondary vLLM port is open in one-worker mode"

  engine="$(state_value worker_0_engine_pid)"
  [[ "$(process_ticks "${engine}")" == "$(state_value worker_0_engine_start_ticks)" && "$(process_uid "${engine}")" == "${SERVICE_UID}" && "$(process_state "${engine}")" != Z ]] || die "engine identity drifted"
  is_descendant "${engine}" "${controller}" || die "GPU engine is not owned by the controller"
  engine_cwd="$(/usr/bin/readlink -e -- "/proc/${engine}/cwd")" || die "cannot inspect engine cwd"
  process_args "${engine}" engine_args || die "cannot inspect engine argv"
  [[ "${engine_cwd}" == "${EXPECTED_CONTROLLER_CWD}" &&
    "$(process_exe "${engine}")" == "$(/usr/bin/readlink -e -- "${EXPECTED_API_PYTHON}")" &&
    "${#engine_args[@]}" -eq 1 && "${engine_args[0]}" == 'VLLM::EngineCore' &&
    "$(process_group "${engine}")" == "${api}" && "$(process_session "${engine}")" == "${api}" ]] ||
    die "engine cwd/argv/process group drifted"
  local apps matching
  apps="$(/usr/bin/nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null)" || die "GPU process inventory failed"
  matching="$(/usr/bin/awk -F, -v uuid="${gpu_uuid}" '{gsub(/[[:space:]]/,"",$1); gsub(/[[:space:]]/,"",$2); if($1==uuid) print $2}' <<<"${apps}")"
  [[ "${matching}" == "${engine}" ]] || die "configured GPU is not exclusively owned by the recorded engine"

  ACTIVE_CONFIG="${active_config}"
  ACTIVE_CONFIG_SHA="${active_hash}"
  CONTROLLER_PID="${controller}"
  ENGINE_PID="${engine}"
  API_PID="${api}"
  API_TICKS="$(process_ticks "${api}")"
  ENGINE_TICKS="$(process_ticks "${engine}")"
  STATE_SHA="$(sha256_file "${SERVICE_STATE}")"
}

check_idle_resources() {
  local links netns tcp udp state_path
  links="$(/usr/sbin/ip -o link show 2>/dev/null)" || die "failed to inventory links"
  netns="$(/usr/sbin/ip netns list 2>/dev/null)" || die "failed to inventory namespaces"
  ! /usr/bin/awk -F': ' '{name=$2; sub(/@.*/,"",name); print name}' <<<"${links}" | /usr/bin/grep -Fxq llmhost0 || die "llmhost0 already exists"
  ! /usr/bin/awk '{print $1}' <<<"${netns}" | /usr/bin/grep -Fxq llm-client || die "llm-client namespace already exists"
  tcp="$(ss_rows -H -ltn)" || die "failed to inspect TCP listeners"
  udp="$(ss_rows -H -lun)" || die "failed to inspect UDP listeners"
  for port in 443 8443 8444 8543 8544; do
    ! /usr/bin/grep -Eq ":${port}[[:space:]]" <<<"${tcp}" || die "protected TCP port ${port} is occupied"
    ! /usr/bin/grep -Eq ":${port}[[:space:]]" <<<"${udp}" || die "protected UDP port ${port} is occupied"
  done
  state_path="${NETWORK_STATE_ROOT}/llm-client.llmhost0.state"
  [[ ! -e "${state_path}" && ! -L "${state_path}" ]] || die "network ownership state already exists"
}

recover_api_key() { process_env_value "${CONTROLLER_PID}" LOCAL_VLLM_API_KEY; }
authenticated_health() {
  local key="$1"
  printf 'Authorization: Bearer %s\n' "${key}" |
    /usr/bin/curl -fsS --max-time 10 --header @- -o /dev/null http://127.0.0.1:8000/v1/models
}
verify_locked_identity() {
  verify_user_file_identity "${SERVICE_STATE_ORIGINAL}" "${SERVICE_STATE_SOURCE_ID}" \
    "${SERVICE_UID}" "${SERVICE_GID}" 384 ||
    die "service state changed while its topology lock was held"
  verify_user_file_identity "${ACTIVE_CONFIG_ORIGINAL}" "${ACTIVE_CONFIG_SOURCE_ID}" \
    "${SERVICE_UID}" "${SERVICE_GID}" 256 ||
    die "active config changed while its topology lock was held"
  [[ "$(sha256_file "${SERVICE_STATE}")" == "${STATE_SHA}" &&
    "$(sha256_file "${ACTIVE_CONFIG}")" == "${ACTIVE_CONFIG_SHA}" &&
    "$(process_ticks "${CONTROLLER_PID}")" == "${CONTROLLER_TICKS}" &&
    "$(process_ticks "${API_PID}")" == "${API_TICKS}" &&
    "$(process_ticks "${ENGINE_PID}")" == "${ENGINE_TICKS}" ]] ||
    die "service identity changed while its topology lock was held"
}
verify_admission() {
  (
    export EXPERIMENT_ENV_FILE="${CONFIG}"
    export PROTOCOL_VALIDATION_ROOT="${PROTOCOL_ROOT}"
    export NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}"
    unset LOCAL_VLLM_API_KEY PROTOCOL_VALIDATION_MARKER
    # Both sourced files are inside the hash-verified, root-owned release.
    source "${SCRIPT_DIR}/lib.sh"
    source "${SCRIPT_DIR}/protocol_admission.sh"
    verify_protocol_admission
  )
}

publish_deferred_admission() {
  local tls_marker http3_marker worker_1_tls_marker worker_1_http3_marker marker
  [[ -f "${ADMISSION_CANDIDATE}" && ! -L "${ADMISSION_CANDIDATE}" &&
    "$(/usr/bin/readlink -e -- "${ADMISSION_CANDIDATE}")" == "${ADMISSION_CANDIDATE}" &&
    "$(/usr/bin/stat -c %u -- "${ADMISSION_CANDIDATE}")" == 0 &&
    "$(/usr/bin/stat -c %a -- "${ADMISSION_CANDIDATE}")" == 444 &&
    "$(/usr/bin/stat -c %h -- "${ADMISSION_CANDIDATE}")" == 1 ]] ||
    die "unsafe deferred-admission candidate"
  [[ "$(candidate_value schema)" == commu-protocol-admission-candidate-v1 ]] ||
    die "wrong deferred-admission candidate schema"
  tls_marker="$(candidate_value tls_pilot_marker)" || die "candidate has no TLS evidence"
  http3_marker="$(candidate_value http3_pilot_marker)" || die "candidate has no HTTP/3 evidence"
  worker_1_tls_marker="$(candidate_value worker_1_tls_pilot_marker)" || die "candidate has no worker-1 TLS field"
  worker_1_http3_marker="$(candidate_value worker_1_http3_pilot_marker)" || die "candidate has no worker-1 HTTP/3 field"
  [[ -z "${worker_1_tls_marker}" && -z "${worker_1_http3_marker}" ]] ||
    die "single-worker candidate unexpectedly names worker-1 evidence"
  for marker in "${tls_marker}" "${http3_marker}"; do
    case "${marker}" in
      "${PROTOCOL_ROOT}"/*/local_vllm_*/PILOT_EVIDENCE_OK.json) ;;
      *) die "candidate evidence path escapes the root-owned pilot hierarchy" ;;
    esac
  done
  (
    export EXPERIMENT_ENV_FILE="${CONFIG}"
    export PROTOCOL_VALIDATION_ROOT="${PROTOCOL_ROOT}"
    export NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}"
    unset LOCAL_VLLM_API_KEY PROTOCOL_VALIDATION_MARKER
    source "${SCRIPT_DIR}/lib.sh"
    source "${SCRIPT_DIR}/protocol_admission.sh"
    write_protocol_success_marker \
      "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" \
      "${tls_marker}" "${http3_marker}" "" ""
  )
  [[ -f "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" &&
    ! -L "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" &&
    "$(/usr/bin/stat -c %u:%h -- "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK")" == 0:1 ]] ||
    die "new admission marker has unsafe identity"
  ADMISSION_MARKER_ID="$(/usr/bin/stat -c %d:%i -- "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK")"
  ADMISSION_PUBLISHED=1
}

release_precheck
load_policy
check_output_hierarchy
acquire_service_lock
pin_service_state
verify_active_service
check_idle_resources

if [[ "${ACTION}" == admission ]]; then
  verify_admission
  verify_locked_identity
  printf 'PRIVILEGED_PROTOCOL_ADMISSION_OK marker=%s/PROTOCOL_VALIDATION_OK\n' "${PROTOCOL_ROOT}"
  exit 0
fi

API_KEY="$(recover_api_key)" || die "could not recover exactly one safe API key from the verified controller"
authenticated_health "${API_KEY}" || die "authenticated vLLM health check failed"
if [[ "${ACTION}" == check ]]; then
  unset API_KEY
  verify_locked_identity
  printf 'PRIVILEGED_PILOT_PRECHECK_OK repository_sha=%s gpu_uuid=%s\n' "${REPOSITORY_SHA}" "${EXPECTED_GPU_UUID}"
  printf 'protocol_root=%s\n' "${PROTOCOL_ROOT}"
  exit 0
fi

verify_locked_identity
ADMISSION_CANDIDATE="${PROTOCOL_ROOT}/.admission-candidate-$$"
[[ ! -e "${ADMISSION_CANDIDATE}" && ! -L "${ADMISSION_CANDIDATE}" ]] ||
  die "deferred-admission candidate already exists"
[[ ! -e "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" &&
  ! -L "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" ]] ||
  die "protocol admission already exists; use the admission action"
EXPERIMENT_ENV_FILE="${CONFIG}" \
PROTOCOL_VALIDATION_ROOT="${PROTOCOL_ROOT}" \
NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}" \
DEFER_PROTOCOL_ADMISSION_PUBLICATION=true \
PROTOCOL_ADMISSION_CANDIDATE="${ADMISSION_CANDIDATE}" \
LOCAL_VLLM_API_KEY="${API_KEY}" \
  /usr/bin/bash -p "${PILOT_SCRIPT}"
unset API_KEY

verify_locked_identity
check_idle_resources
verify_active_service
verify_locked_identity
publish_deferred_admission
verify_active_service
verify_locked_identity
verify_admission
verify_active_service
verify_locked_identity
rm -- "${ADMISSION_CANDIDATE}"
ADMISSION_CANDIDATE=""
printf 'PRIVILEGED_PROTOCOL_PILOTS_OK marker=%s/PROTOCOL_VALIDATION_OK\n' "${PROTOCOL_ROOT}"
