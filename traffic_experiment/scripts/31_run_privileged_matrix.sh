#!/usr/bin/bash -p
set -euo pipefail
set +x
umask 077

# Supervise the fixed one-GPU, 2,808-call matrix from an installed root-owned
# release. The protocol pilots remain a separate release and launcher.

EXPECTED_CONTROLLER_CWD=/home/wongshingyin
EXPECTED_CONTROLLER_EXE=/usr/bin/bash
EXPECTED_CONTROLLER_LAUNCHER=/home/wongshingyin/commu/traffic_experiment/scripts/03_start_vllm_dual.sh
EXPECTED_API_PYTHON=/home/wongshingyin/.venvs/commu-qwen35-e12240f/bin/python
EXPECTED_API_VLLM=/home/wongshingyin/.venvs/commu-qwen35-e12240f/bin/vllm
EXPECTED_API_LD_LIBRARY_PATH=/home/wongshingyin/.venvs/commu-qwen35-e12240f/lib/python3.12/site-packages/nvidia/cu13/lib:/home/wongshingyin/.venvs/commu-qwen35-e12240f/lib/python3.12/site-packages/torch/lib
EXPECTED_SERVICE_USER=wongshingyin
EXPECTED_SERVICE_UID=1007
EXPECTED_SERVICE_GID=1007
EXPECTED_SERVICE_STATE_ROOT=/home/wongshingyin/.config/commu

FIXED_PATH=/usr/sbin:/usr/bin
if [[ "${COMMU_PRIVILEGED_MATRIX_CLEAN_ENV:-}" != 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'ERROR: runner must run as root\n' >&2; exit 2; }
  SELF="$(/usr/bin/readlink -e -- "$0")" || exit 2
  [[ -f "${SELF}" && ! -L "${SELF}" && "$(/usr/bin/stat -c %u -- "${SELF}")" == 0 ]] || exit 2
  exec /usr/bin/env -i \
    COMMU_PRIVILEGED_MATRIX_CLEAN_ENV=1 \
    HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
    /usr/bin/bash -p "${SELF}" "$@"
fi
PATH="${FIXED_PATH}"
export PATH HOME LANG LC_ALL TZ PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE PYTHONSAFEPATH

# The sentinel is not a trust decision. Even if a caller spells it manually,
# no ambient variable outside this harmless allowlist may survive.
while IFS='=' read -r inherited_name _; do
  case "${inherited_name}" in
    COMMU_PRIVILEGED_MATRIX_CLEAN_ENV|HOME|LANG|LC_ALL|TZ|PATH|PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|PYTHONSAFEPATH|PWD|SHLVL|_) ;;
    *) printf 'ERROR: unsanitized environment variable: %s\n' "${inherited_name}" >&2; exit 2 ;;
  esac
done < <(/usr/bin/env)
[[ "${EUID}" -eq 0 ]] || { printf 'ERROR: runner must run as root\n' >&2; exit 2; }
cd /
# Bash exports OLDPWD when cd runs. The ambient scan above has already rejected
# a caller-supplied value, so remove only this shell-created value before any
# child process can inherit it.
unset OLDPWD
[[ "${HOME}" == /root && "${LANG}" == C.UTF-8 && "${LC_ALL}" == C.UTF-8 &&
  "${TZ}" == UTC && "${PATH}" == "${FIXED_PATH}" &&
  "${PYTHONNOUSERSITE}" == 1 && "${PYTHONDONTWRITEBYTECODE}" == 1 &&
  "${PYTHONSAFEPATH}" == 1 && "${PWD}" == / ]] || {
  printf 'ERROR: sanitized environment values do not match the fixed policy\n' >&2
  exit 2
}

ACTION="${1:-}"
shift || true
SERVICE_STATE_REQUESTED=""
RUN_ID=""
while (($#)); do
  case "$1" in
    --service-state) [[ $# -ge 2 && -z "${SERVICE_STATE_REQUESTED}" ]] || break; SERVICE_STATE_REQUESTED="$2"; shift 2 ;;
    --run-id) [[ $# -ge 2 && -z "${RUN_ID}" ]] || break; RUN_ID="$2"; shift 2 ;;
    *) break ;;
  esac
done
case "${ACTION}" in check|status|run|resume) ;;
  *) ACTION="" ;;
esac
if [[ -z "${ACTION}" || -z "${SERVICE_STATE_REQUESTED}" ||
  ! "${RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ || $# -ne 0 ]]; then
  printf 'usage: %s {check|status|run|resume} --service-state /absolute/path/to/service.state --run-id SAFE_ID\n' "$0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/.." && pwd)"
RELEASE_ROOT="$(cd -- "${REPOSITORY_ROOT}/.." && pwd)"
CONFIG_TEMPLATE="${RELEASE_ROOT}/config/server.env"
CONFIG=""
POLICY="${RELEASE_ROOT}/policy/service.state"
METADATA="${RELEASE_ROOT}/RELEASE_METADATA"
MANIFEST="${RELEASE_ROOT}/RELEASE_FILES.sha256"
RUNTIME_MANIFEST="${RELEASE_ROOT}/INSTALLED_RUNTIME_FILES.sha256"
VALIDATOR="${SCRIPT_DIR}/privileged_matrix_config.py"
CADDY_READINESS="${SCRIPT_DIR}/caddy_readiness.py"
RUNNER_PYTHON="${EXPERIMENT_ROOT}/.venv-runner/bin/python"
CADDY="${EXPERIMENT_ROOT}/.tools/caddy"
DUMPCAP=/usr/bin/dumpcap
DUMPCAP_GID=""
LOCK_HELD=0
LOCK_ID=""
SERVICE_LOCK_DIR=""
GLOBAL_LOCK_FILE=""
SERVICE_STATE_ORIGINAL=""
SERVICE_STATE_SNAPSHOT=""
SERVICE_STATE_SOURCE_ID=""
ACTIVE_CONFIG_ORIGINAL=""
ACTIVE_CONFIG_SNAPSHOT=""
ACTIVE_CONFIG_SOURCE_ID=""
BASE_OUTPUT_ROOT=""
OUTPUT_ROOT=""
RUNTIME_CONFIG=""
PROTOCOL_ROOT=""
NETWORK_STATE_ROOT=""

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

verify_dumpcap_identity() {
  local observed_gid group_record capabilities
  [[ "$(root_command dumpcap)" == "${DUMPCAP}" ]] || return 1
  regular_root_file "${DUMPCAP}" || return 1
  [[ "$(/usr/bin/stat -c %u:%a:%h -- "${DUMPCAP}")" == 0:754:1 ]] ||
    return 1
  observed_gid="$(/usr/bin/stat -c %g -- "${DUMPCAP}")" || return 1
  [[ "${observed_gid}" =~ ^[1-9][0-9]*$ ]] || return 1
  if [[ -n "${DUMPCAP_GID}" && "${observed_gid}" != "${DUMPCAP_GID}" ]]; then
    return 1
  fi
  DUMPCAP_GID="${observed_gid}"
  group_record="$(/usr/bin/getent group "${DUMPCAP_GID}")" || return 1
  [[ "${group_record%%:*}" == wireshark ]] || return 1
  capabilities="$(/usr/sbin/getcap -- "${DUMPCAP}")" || return 1
  [[ "${capabilities}" == "${DUMPCAP} cap_net_admin,cap_net_raw=eip" ]]
}

verify_dumpcap_access() {
  verify_dumpcap_identity || return 1
  /usr/bin/id -G "${SERVICE_USER}" |
    /usr/bin/awk -v gid="${DUMPCAP_GID}" '
      {for (field_number = 1; field_number <= NF; field_number++) if ($field_number == gid) found = 1}
      END {exit !found}
    ' || return 1
  /usr/bin/setpriv --reuid "${SERVICE_UID}" --regid "${SERVICE_GID}" \
    --groups "${DUMPCAP_GID}" /usr/bin/bash -p -c '[[ -x "$1" ]]' \
    dumpcap-exec-check "${DUMPCAP}"
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
udp_port_closed() { local rows; rows="$(ss_rows -H -lun "sport = :$1")" || return 1; [[ -z "${rows}" ]]; }
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
  for directory in / /home /opt /opt/commu-secure-matrix \
    /opt/commu-secure-matrix/releases /usr /usr/bin /usr/sbin \
    /var /var/lib /run; do
    trusted_root_directory "${directory}" || die "unsafe privileged path component: ${directory}"
  done
  trusted_root_directory /run/lock 1777 || die "unsafe system lock directory"
  trusted_root_directory /run/lock/commu-protocol-pilots 755 ||
    die "unsafe project lock directory"
  for file in "${CONFIG_TEMPLATE}" "${POLICY}" "${METADATA}" "${MANIFEST}" "${RUNTIME_MANIFEST}" \
    "${VALIDATOR}" "${SCRIPT_DIR}/privileged_matrix_state.py" \
    "${SCRIPT_DIR}/privileged_matrix_request.py" "${CADDY_READINESS}" "${RUNNER_PYTHON}" "${CADDY}"; do
    regular_root_file "${file}" || die "unsafe release file: ${file}"
  done
  case "${RELEASE_ROOT}" in /opt/commu-secure-matrix/releases/[0-9a-f][0-9a-f]*) ;; *) die "release is outside the fixed installation root" ;; esac
  for command_name in awk bash chmod curl cut date dumpcap ethtool find flock getcap getent grep hostname id ip iperf3 mkdir nvidia-smi python3 readlink rm rmdir sed setpriv sha256sum sleep sort ss stat sync tail tc tee tshark uname wc; do
    root_command "${command_name}" >/dev/null || die "unsafe or missing system command: ${command_name}"
  done
  verify_dumpcap_identity || die "system dumpcap identity or capabilities drifted"
  [[ -x /usr/bin/kill && "$(/usr/bin/stat -c %u -- /usr/bin/kill)" == 0 ]] ||
    die "unsafe or missing /usr/bin/kill"
  (
    cd "${RELEASE_ROOT}"
    /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
    /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
  ) || die "release file digest verification failed"
  [[ "$(metadata_value schema)" == commu-privileged-matrix-release-v1 ]] || die "wrong release schema"
  [[ "$(metadata_value purpose)" == secure-single-gpu-full-matrix ]] || die "release is not the full-matrix release"
  REPOSITORY_SHA="$(metadata_value repository_sha)" || die "missing repository SHA"
  [[ "${REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "invalid repository SHA"
  [[ "$(basename -- "${RELEASE_ROOT}")" == "${REPOSITORY_SHA}" ]] || die "release directory/SHA mismatch"
  for directory in "${RELEASE_ROOT}" "${RELEASE_ROOT}/repository" \
    "${EXPERIMENT_ROOT}" "${SCRIPT_DIR}"; do
    trusted_root_directory "${directory}" || die "unsafe release directory: ${directory}"
  done
  /usr/bin/python3 -I "${VALIDATOR}" check \
    --input "${CONFIG_TEMPLATE}" --repository-sha "${REPOSITORY_SHA}" --release-root "${RELEASE_ROOT}" ||
    die "privileged config validation failed"
}

load_policy() {
  [[ "$(policy_value schema)" == commu-privileged-matrix-policy-v2 ]] || die "wrong policy schema"
  [[ "$(policy_value repository_sha)" == "${REPOSITORY_SHA}" ]] || die "policy/release SHA mismatch"
  SERVICE_STATE_ROOT="$(policy_value service_state_root)" || die "missing service-state root"
  SERVICE_USER="$(policy_value service_user)" || die "missing service user"
  SERVICE_UID="$(policy_value service_uid)" || die "missing service UID"
  SERVICE_GID="$(policy_value service_gid)" || die "missing service GID"
  PILOT_REPOSITORY_SHA="$(policy_value pilot_repository_sha)" || die "missing pilot repository SHA"
  [[ "${SERVICE_USER}" == "${EXPECTED_SERVICE_USER}" &&
    "${SERVICE_UID}" == "${EXPECTED_SERVICE_UID}" &&
    "${SERVICE_GID}" == "${EXPECTED_SERVICE_GID}" &&
    "${SERVICE_STATE_ROOT}" == "${EXPECTED_SERVICE_STATE_ROOT}" &&
    "${PILOT_REPOSITORY_SHA}" == "${REPOSITORY_SHA}" ]] ||
    die "installed service policy is outside the reviewed scope"
  [[ "${SERVICE_STATE_ROOT}" = /* && "${SERVICE_STATE_ROOT}" != *$'\n'* ]] || die "unsafe service-state root"
  [[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*$ && "${SERVICE_UID}" =~ ^[1-9][0-9]*$ && "${SERVICE_GID}" =~ ^[1-9][0-9]*$ ]] || die "unsafe service identity policy"
  [[ "$(/usr/bin/id -u "${SERVICE_USER}")" == "${SERVICE_UID}" && "$(/usr/bin/id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] || die "service account identity drifted"
  verify_dumpcap_access ||
    die "service account cannot use the fixed system dumpcap"
  [[ -d "${SERVICE_STATE_ROOT}" && ! -L "${SERVICE_STATE_ROOT}" &&
    "$(/usr/bin/readlink -e -- "${SERVICE_STATE_ROOT}")" == "${SERVICE_STATE_ROOT}" &&
    "$(/usr/bin/stat -c %u -- "${SERVICE_STATE_ROOT}")" == "${SERVICE_UID}" ]] ||
    die "unsafe service-state root"
  SERVICE_STATE="$(/usr/bin/readlink -e -- "${SERVICE_STATE_REQUESTED}")" ||
    die "selected service state does not exist"
  case "${SERVICE_STATE}" in
    "${SERVICE_STATE_ROOT}"/qwen35-[A-Za-z0-9._-]*/service-[A-Za-z0-9._-]*.state) ;;
    *) die "selected service state is outside the reviewed state hierarchy" ;;
  esac
}

check_base_output_hierarchy() {
  local root="/var/lib/commu-secure-matrix/${REPOSITORY_SHA}" path
  [[ -d /var/lib/commu-secure-matrix && ! -L /var/lib/commu-secure-matrix &&
    "$(/usr/bin/stat -c %u:%g:%a -- /var/lib/commu-secure-matrix)" == "0:${SERVICE_GID}:710" ]] ||
    die "unsafe root-owned matrix output base"
  for path in "${root}" "${root}/snapshots"; do
    [[ -d "${path}" && ! -L "${path}" &&
      "$(/usr/bin/stat -c %u:%g:%a -- "${path}")" == "0:${SERVICE_GID}:710" ]] ||
      die "unsafe root-owned output directory: ${path}"
  done
  BASE_OUTPUT_ROOT="${root}"
}

select_gpu_output_hierarchy() {
  local gpu_index gpu_uuid actual_uuid scope path
  gpu_index="$(state_value worker_0_gpu_index)" || die "service state has no GPU index"
  gpu_uuid="$(state_value worker_0_gpu_uuid)" || die "service state has no GPU UUID"
  [[ "${gpu_index}" =~ ^(0|[1-9][0-9]*)$ && "${gpu_uuid}" =~ ^GPU-[0-9A-Fa-f-]+$ ]] ||
    die "service state has unsafe GPU identity"
  actual_uuid="$(/usr/bin/nvidia-smi -i "${gpu_index}" --query-gpu=uuid --format=csv,noheader,nounits 2>/dev/null)" ||
    die "GPU inventory failed"
  actual_uuid="${actual_uuid//[[:space:]]/}"
  [[ "${actual_uuid}" == "${gpu_uuid}" ]] || die "selected GPU index/UUID mapping drifted"
  EXPECTED_GPU_INDEX="${gpu_index}"
  EXPECTED_GPU_UUID="${gpu_uuid}"
  scope="gpu-${gpu_index}-${gpu_uuid}"
  OUTPUT_ROOT="${BASE_OUTPUT_ROOT}/${scope}"
  for path in "${OUTPUT_ROOT}" "${OUTPUT_ROOT}/runs" "${OUTPUT_ROOT}/caddy"; do
    if [[ ! -e "${path}" && ! -L "${path}" ]]; then
      /usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${path}"
    fi
    [[ -d "${path}" && ! -L "${path}" &&
      "$(/usr/bin/stat -c %u:%g:%a -- "${path}")" == "0:${SERVICE_GID}:710" ]] ||
      die "unsafe GPU-scoped output directory: ${path}"
  done
  RUNTIME_CONFIG="${OUTPUT_ROOT}/.runtime-config-$$"
  /usr/bin/python3 -I "${VALIDATOR}" materialize \
    --input "${CONFIG_TEMPLATE}" --output "${RUNTIME_CONFIG}" \
    --repository-sha "${REPOSITORY_SHA}" --gpu-index "${gpu_index}" --gpu-uuid "${gpu_uuid}" ||
    die "could not materialize GPU-scoped matrix config"
  regular_root_file "${RUNTIME_CONFIG}" || die "unsafe GPU-scoped matrix config"
  CONFIG="${RUNTIME_CONFIG}"
  PROTOCOL_ROOT="/var/lib/commu-protocol-pilots/${PILOT_REPOSITORY_SHA}/${scope}/runs/protocol_validation"
  NETWORK_STATE_ROOT="${OUTPUT_ROOT}/network_state"
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
  SERVICE_STATE_SNAPSHOT="${BASE_OUTPUT_ROOT}/snapshots/.service-state-$$"
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
  local status="${1:-$?}"
  local behavior="${2:-exit}"
  trap - EXIT
  for snapshot in "${RUNTIME_CONFIG}" "${ACTIVE_CONFIG_SNAPSHOT}" "${SERVICE_STATE_SNAPSHOT}"; do
    [[ -n "${snapshot}" ]] || continue
    case "${snapshot}" in
      "${OUTPUT_ROOT}"/.runtime-config-[0-9]*|\
      "${OUTPUT_ROOT}"/.active-config-[0-9]*|\
      "${BASE_OUTPUT_ROOT}"/snapshots/.service-state-[0-9]*) ;;
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
  if [[ "${behavior}" == return ]]; then
    return "${status}"
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM HUP

verify_active_service() {
  local state_parent active_config active_hash controller api engine gpu_index gpu_uuid
  local active_vllm active_ld controller_cwd api_cwd engine_cwd engine_title engine_arg_index
  local -a controller_args=() api_args=() engine_args=() expected_api_args=()
  state_parent="$(dirname -- "${SERVICE_STATE_ORIGINAL}")"
  regular_root_file "${SERVICE_STATE}" || die "unsafe pinned service state"
  [[ "$(state_value schema)" == commu-vllm-service-state-v2 && "$(state_value status)" == running ]] || die "service state is not a running v2 record"
  [[ "$(state_value repository_sha)" == "${PILOT_REPOSITORY_SHA}" ]] ||
    die "service repository SHA differs from the admitted pilot release"
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
  [[ "${#engine_args[@]}" -ge 1 ]] || die "engine cwd/argv/process group drifted"
  engine_title="${engine_args[0]}"
  while [[ "${engine_title}" == *" " ]]; do engine_title="${engine_title% }"; done
  for ((engine_arg_index=1;engine_arg_index<${#engine_args[@]};engine_arg_index++)); do
    [[ -z "${engine_args[engine_arg_index]}" ]] || die "engine cwd/argv/process group drifted"
  done
  [[ "${engine_cwd}" == "${EXPECTED_CONTROLLER_CWD}" &&
    "$(process_exe "${engine}")" == "$(/usr/bin/readlink -e -- "${EXPECTED_API_PYTHON}")" &&
    "${engine_title}" == 'VLLM::EngineCore' &&
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
  local marker="${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK" path protocol_runs protocol_gpu_root
  local pilot_release_root pilot_experiment_root pilot_manifest manifest_relative
  protocol_runs="$(/usr/bin/dirname -- "${PROTOCOL_ROOT}")"
  protocol_gpu_root="$(/usr/bin/dirname -- "${protocol_runs}")"
  for path in /var/lib/commu-protocol-pilots \
    "/var/lib/commu-protocol-pilots/${PILOT_REPOSITORY_SHA}" \
    "${protocol_gpu_root}" "${protocol_runs}" \
    "${PROTOCOL_ROOT}"; do
    trusted_root_directory "${path}" ||
      die "unsafe protocol-admission path component: ${path}"
  done
  pilot_release_root="/opt/commu-protocol-pilots/releases/${PILOT_REPOSITORY_SHA}"
  pilot_experiment_root="${pilot_release_root}/repository/traffic_experiment"
  manifest_relative="$(config_value "${CONFIG}" MANIFEST_PATH)" ||
    die "matrix config has no QA manifest path"
  [[ "${manifest_relative}" == artifacts/requests_32.jsonl ]] ||
    die "matrix QA manifest path is outside the reviewed scope"
  pilot_manifest="${pilot_experiment_root}/${manifest_relative}"
  for path in /opt/commu-protocol-pilots /opt/commu-protocol-pilots/releases \
    "${pilot_release_root}" "${pilot_release_root}/repository" \
    "${pilot_experiment_root}" "${pilot_experiment_root}/artifacts"; do
    trusted_root_directory "${path}" ||
      die "unsafe pilot-release path component: ${path}"
  done
  regular_root_file "${pilot_manifest}" ||
    die "pilot QA manifest is not an immutable root-owned regular file"
  [[ "$(sha256_file "${pilot_manifest}")" == "$(config_value "${CONFIG}" MANIFEST_SHA256)" ]] ||
    die "pilot and matrix QA manifests differ"
  regular_root_file "${marker}" ||
    die "protocol admission is not an immutable root-owned regular file"
  [[ "$(/usr/bin/stat -c %a -- "${marker}")" == 444 ]] ||
    die "protocol admission mode is not immutable"
  (
    export EXPERIMENT_ENV_FILE="${CONFIG}"
    export PROTOCOL_VALIDATION_ROOT="${PROTOCOL_ROOT}"
    export NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}"
    unset LOCAL_VLLM_API_KEY PROTOCOL_VALIDATION_MARKER
    # Both sourced files are inside the hash-verified, root-owned matrix release.
    source "${SCRIPT_DIR}/lib.sh"
    source "${SCRIPT_DIR}/protocol_admission.sh"
    # Admission evidence belongs to the separately installed pilot release.
    # Rebase both paths that carry release-local provenance while retaining the
    # matrix config's independently verified content digests and fixed plan.
    RUNS_ROOT="${protocol_runs}"
    MANIFEST_PATH="${pilot_manifest}"
    verify_protocol_admission
  )
}

STATE_TOOL="${SCRIPT_DIR}/privileged_matrix_state.py"
REQUEST_TOOL="${SCRIPT_DIR}/privileged_matrix_request.py"
MATRIX_ROOT=""
NETWORK_OWNED=0
CADDY_OWNED=0
CADDY_PID=""
CADDY_TICKS=""
CADDY_CONFIG="${EXPERIMENT_ROOT}/configs/Caddyfile.single"
CADDY_STATE=""
CADDY_LOG=""

network_state_file() {
  printf '%s/llm-client.llmhost0.state\n' "${NETWORK_STATE_ROOT}"
}

verify_network_inventory() {
  local expected="$1" links netns state condition host_qdisc client_qdisc
  local host_features client_features
  links="$(/usr/sbin/ip -o link show 2>/dev/null)" || die "failed to inventory links"
  netns="$(/usr/sbin/ip netns list 2>/dev/null)" || die "failed to inventory namespaces"
  /usr/bin/awk -F': ' '{name=$2; sub(/@.*/,"",name); print name}' <<<"${links}" |
    /usr/bin/grep -Fxq llmhost0 || die "owned host veth is absent"
  /usr/bin/awk '{print $1}' <<<"${netns}" | /usr/bin/grep -Fxq llm-client ||
    die "owned client namespace is absent"
  state="$(network_state_file)"
  regular_root_file "${state}" || die "unsafe network ownership state"
  condition="$(/usr/bin/awk -F= '$1=="condition"{print $2}' "${state}")"
  [[ "${condition}" == "${expected}" &&
    "$(/usr/bin/awk -F= '$1=="status"{print $2}' "${state}")" == active ]] ||
    die "network state differs from requested condition"
  /usr/sbin/ip -details link show dev llmhost0 >/dev/null 2>&1 ||
    die "host-veth inventory failed"
  /usr/sbin/ip netns exec llm-client /usr/sbin/ip -details link show dev llmclient0 >/dev/null 2>&1 ||
    die "client-veth inventory failed"
  /usr/sbin/ip -o link show dev llmhost0 | /usr/bin/grep -Eq ' mtu 1500 ' ||
    die "host MTU differs from 1500"
  /usr/sbin/ip netns exec llm-client /usr/sbin/ip -o link show dev llmclient0 |
    /usr/bin/grep -Eq ' mtu 1500 ' || die "client MTU differs from 1500"
  /usr/sbin/ip -o address show dev llmhost0 |
    /usr/bin/grep -Eq ' inet 10\.200\.0\.1/24 ' || die "host address drifted"
  /usr/sbin/ip netns exec llm-client /usr/sbin/ip -o address show dev llmclient0 |
    /usr/bin/grep -Eq ' inet 10\.200\.0\.2/24 ' || die "client address drifted"
  host_features="$(/usr/sbin/ethtool -k llmhost0 2>/dev/null)" ||
    die "host offload inventory failed"
  client_features="$(/usr/sbin/ip netns exec llm-client /usr/sbin/ethtool -k llmclient0 2>/dev/null)" ||
    die "client offload inventory failed"
  for feature in tcp-segmentation-offload generic-segmentation-offload \
    generic-receive-offload large-receive-offload; do
    /usr/bin/grep -Eq "^${feature}: off([[:space:]]|$)" <<<"${host_features}" ||
      die "host offload ${feature} is not disabled"
    /usr/bin/grep -Eq "^${feature}: off([[:space:]]|$)" <<<"${client_features}" ||
      die "client offload ${feature} is not disabled"
  done
  host_qdisc="$(/usr/sbin/tc qdisc show dev llmhost0 2>/dev/null)" ||
    die "host qdisc inventory failed"
  client_qdisc="$(/usr/sbin/ip netns exec llm-client /usr/sbin/tc qdisc show dev llmclient0 2>/dev/null)" ||
    die "client qdisc inventory failed"
  case "${expected}" in
    baseline)
      [[ "${host_qdisc}" != *"netem"* && "${client_qdisc}" != *"netem"* ]] ||
        die "baseline unexpectedly has netem"
      ;;
    rtt)
      [[ "${host_qdisc}" == *"netem"* && "${host_qdisc}" == *"delay 20ms"* &&
        "${client_qdisc}" == *"netem"* && "${client_qdisc}" == *"delay 20ms"* ]] ||
        die "rtt qdisc differs from the fixed plan"
      ;;
    realistic)
      [[ "${host_qdisc}" == *"netem"* && "${host_qdisc}" == *"delay 20ms"* &&
        "${host_qdisc}" == *"rate 50Mbit"* &&
        "${client_qdisc}" == *"netem"* && "${client_qdisc}" == *"delay 20ms"* &&
        "${client_qdisc}" == *"rate 20Mbit"* ]] ||
        die "realistic qdisc differs from the fixed plan"
      ;;
  esac
}

apply_network() {
  check_idle_resources
  CLIENT_NETNS=llm-client HOST_VETH=llmhost0 CLIENT_VETH=llmclient0 \
  HOST_VETH_CIDR=10.200.0.1/24 CLIENT_VETH_CIDR=10.200.0.2/24 \
  NETWORK_MTU=1500 NETWORK_RTT_MS=40 NETWORK_UPLINK_MBIT=20 \
  NETWORK_DOWNLINK_MBIT=50 NETWORK_QUEUE_PACKETS=1000 \
  NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}" \
    /usr/bin/bash -p "${SCRIPT_DIR}/11_network_condition.sh" apply "$1"
  NETWORK_OWNED=1
  verify_network_inventory "$1"
}

cleanup_network() {
  [[ "${NETWORK_OWNED}" -eq 1 ]] || return 0
  CLIENT_NETNS=llm-client HOST_VETH=llmhost0 CLIENT_VETH=llmclient0 \
  NETWORK_STATE_DIR="${NETWORK_STATE_ROOT}" \
    /usr/bin/bash -p "${SCRIPT_DIR}/11_network_condition.sh" reset ||
    return 1
  NETWORK_OWNED=0
  check_idle_resources
}

caddy_process_matches() {
  local -a actual_args=() expected_args=()
  [[ "${CADDY_OWNED}" -eq 1 && "${CADDY_PID}" =~ ^[0-9]+$ &&
    "$(process_ticks "${CADDY_PID}")" == "${CADDY_TICKS}" &&
    "$(process_uid "${CADDY_PID}")" == 0 &&
    "$(process_exe "${CADDY_PID}")" == "${CADDY}" ]] || return 1
  process_args "${CADDY_PID}" actual_args || return 1
  expected_args=("${CADDY}" run --config "${CADDY_CONFIG}" --adapter caddyfile)
  arrays_equal actual_args expected_args
}

verify_caddy_process() {
  caddy_process_matches || die "Caddy process identity/argv drifted"
}

caddy_listeners_owned() {
  local tcp udp tcp_pids udp_pids
  tcp="$(ss_rows -H -ltnp 'sport = :8443')" || return 1
  udp="$(ss_rows -H -lunp 'sport = :8444')" || return 1
  tcp_pids="$(/usr/bin/grep -oE 'pid=[0-9]+' <<<"${tcp}" | /usr/bin/cut -d= -f2 | /usr/bin/sort -u)"
  udp_pids="$(/usr/bin/grep -oE 'pid=[0-9]+' <<<"${udp}" | /usr/bin/cut -d= -f2 | /usr/bin/sort -u)"
  [[ "${tcp_pids}" == "${CADDY_PID}" && "${udp_pids}" == "${CADDY_PID}" &&
    "${tcp}" == *"10.200.0.1:8443"* && "${udp}" == *"10.200.0.1:8444"* ]] ||
    return 1
  port_closed 8543 || return 1
  udp_port_closed 8544 || return 1
}

verify_caddy() {
  verify_caddy_process
  caddy_listeners_owned || die "Caddy listeners are absent or owned by another process"
}

caddy_namespace_ready() {
  local ca_file="$1"
  /usr/sbin/ip netns exec llm-client \
    "${RUNNER_PYTHON}" -I "${CADDY_READINESS}" \
    --host 10.200.0.1 --ca-file "${ca_file}" \
    --tls-port 8443 --http3-port 8444 --timeout-seconds 1
}

wait_for_caddy_ready() {
  local ca_file="$1" attempt
  for ((attempt=0; attempt<60; attempt++)); do
    caddy_process_matches || return 1
    if regular_root_file "${ca_file}" && caddy_listeners_owned &&
      caddy_namespace_ready "${ca_file}" >/dev/null 2>&1; then
      return 0
    fi
    /usr/bin/sleep .1
  done
  caddy_namespace_ready "${ca_file}" || true
  return 1
}

caddy_listeners_closed() {
  port_closed 8443 && udp_port_closed 8444 &&
    port_closed 8543 && udp_port_closed 8544
}

caddy_recorded_process_exited() {
  local ticks state
  [[ -d "/proc/${CADDY_PID}" ]] || return 0
  if ! ticks="$(process_ticks "${CADDY_PID}" 2>/dev/null)"; then
    [[ ! -d "/proc/${CADDY_PID}" ]] && return 0
    return 1
  fi
  if ! state="$(process_state "${CADDY_PID}" 2>/dev/null)"; then
    [[ ! -d "/proc/${CADDY_PID}" ]] && return 0
    return 1
  fi
  [[ "${ticks}" != "${CADDY_TICKS}" || "${state}" == Z ]]
}

finalize_stopped_caddy() {
  caddy_listeners_closed || return 1
  [[ -f "${CADDY_STATE}" && ! -L "${CADDY_STATE}" &&
    "$(/usr/bin/stat -c %u:%a:%h -- "${CADDY_STATE}")" == 0:400:1 ]] || return 1
  /usr/bin/rm -- "${CADDY_STATE}" || return 1
  CADDY_OWNED=0
  CADDY_PID=""
  CADDY_TICKS=""
  CADDY_STATE=""
  CADDY_LOG=""
}

start_caddy() {
  local data="${OUTPUT_ROOT}/caddy/data" config="${OUTPUT_ROOT}/caddy/config" attempt
  [[ -f "${CADDY_CONFIG}" && ! -L "${CADDY_CONFIG}" ]] ||
    die "single-worker Caddy config is unsafe"
  CADDY_STATE="${OUTPUT_ROOT}/caddy/matrix-caddy.state"
  CADDY_LOG="${OUTPUT_ROOT}/caddy/matrix-caddy.log"
  [[ ! -e "${CADDY_STATE}" && ! -L "${CADDY_STATE}" ]] ||
    die "Caddy state already exists"
  HOME=/root XDG_DATA_HOME="${data}" XDG_CONFIG_HOME="${config}" \
  VLLM_HOST=127.0.0.1 VLLM_PORT=8000 VLLM_SECONDARY_PORT=8001 \
  SECURE_PROXY_HOST=10.200.0.1 \
    "${CADDY}" validate --config "${CADDY_CONFIG}" --adapter caddyfile
  HOME=/root XDG_DATA_HOME="${data}" XDG_CONFIG_HOME="${config}" \
  VLLM_HOST=127.0.0.1 VLLM_PORT=8000 VLLM_SECONDARY_PORT=8001 \
  SECURE_PROXY_HOST=10.200.0.1 \
    "${CADDY}" run --config "${CADDY_CONFIG}" --adapter caddyfile >>"${CADDY_LOG}" 2>&1 &
  CADDY_PID=$!
  for ((attempt=0; attempt<100; attempt++)); do
    CADDY_TICKS="$(process_ticks "${CADDY_PID}" 2>/dev/null || true)"
    [[ -n "${CADDY_TICKS}" ]] && break
    /usr/bin/sleep .01
  done
  if [[ -z "${CADDY_TICKS}" ]]; then
    wait "${CADDY_PID}" 2>/dev/null || true
    die "could not record Caddy identity"
  fi
  CADDY_OWNED=1
  {
    printf 'schema=commu-secure-matrix-caddy-v1\n'
    printf 'pid=%s\nstart_ticks=%s\nexe=%s\nconfig=%s\n' \
      "${CADDY_PID}" "${CADDY_TICKS}" "${CADDY}" "${CADDY_CONFIG}"
  } >"${CADDY_STATE}"
  /usr/bin/chmod 0400 "${CADDY_STATE}"
  local generated_ca="${data}/caddy/pki/authorities/local/root.crt"
  wait_for_caddy_ready "${generated_ca}" ||
    die "Caddy did not become ready; see ${CADDY_LOG}"
  /usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${MATRIX_ROOT}/ca"
  CA_FILE="${MATRIX_ROOT}/ca/root.crt"
  if [[ -e "${CA_FILE}" || -L "${CA_FILE}" ]]; then
    regular_root_file "${CA_FILE}" || die "matrix CA snapshot is unsafe"
    [[ "$(sha256_file "${CA_FILE}")" == "$(sha256_file "${generated_ca}")" ]] ||
      die "Caddy CA identity changed between network cells"
  else
    /usr/bin/install -o root -g "${SERVICE_GID}" -m 0440 "${generated_ca}" "${CA_FILE}"
    [[ "$(sha256_file "${CA_FILE}")" == "$(sha256_file "${generated_ca}")" ]] ||
      die "Caddy CA snapshot digest mismatch"
  fi
}

stop_caddy() {
  local attempt
  [[ "${CADDY_OWNED}" -eq 1 ]] || return 0
  if caddy_recorded_process_exited; then
    wait "${CADDY_PID}" 2>/dev/null || true
    finalize_stopped_caddy
    return
  fi
  if ! caddy_process_matches; then
    printf 'ERROR: recorded Caddy PID is live but its identity is ambiguous; preserving state\n' >&2
    return 1
  fi
  /usr/bin/kill -TERM "${CADDY_PID}" || return 1
  for ((attempt=0; attempt<100; attempt++)); do
    [[ "$(process_state "${CADDY_PID}" 2>/dev/null || true)" == Z ||
      "$(process_ticks "${CADDY_PID}" 2>/dev/null || true)" != "${CADDY_TICKS}" ]] && break
    /usr/bin/sleep .1
  done
  [[ "$(process_state "${CADDY_PID}" 2>/dev/null || true)" == Z ||
    "$(process_ticks "${CADDY_PID}" 2>/dev/null || true)" != "${CADDY_TICKS}" ]] ||
    return 1
  wait "${CADDY_PID}" 2>/dev/null || true
  finalize_stopped_caddy
}

matrix_cleanup() {
  local status=$? failed=0
  trap - EXIT INT TERM HUP
  if ! stop_caddy; then
    failed=1
  elif ! cleanup_network; then
    failed=1
  fi
  if [[ "${failed}" -ne 0 ]]; then
    printf 'ERROR: exact owned cleanup failed; state was preserved\n' >&2
    [[ "${status}" -ne 0 ]] || status=1
  fi
  cleanup "${status}"
}

state_args() {
  printf '%s\0' \
    --root "${MATRIX_ROOT}" \
    --repository-sha "${REPOSITORY_SHA}" \
    --pilot-repository-sha "${PILOT_REPOSITORY_SHA}" \
    --release-files-sha256 "$(sha256_file "${MANIFEST}")" \
    --config-sha256 "$(sha256_file "${CONFIG}")" \
    --admission-sha256 "$(sha256_file "${PROTOCOL_ROOT}/PROTOCOL_VALIDATION_OK")" \
    --service-state-sha256 "${STATE_SHA}" \
    --active-config-sha256 "${ACTIVE_CONFIG_SHA}" \
    --qa-manifest-sha256 "$(config_value "${CONFIG}" MANIFEST_SHA256)" \
    --summary-manifest-sha256 "$(config_value "${CONFIG}" SUMMARY_MANIFEST_SHA256)" \
    --run-id "${RUN_ID}" \
    --gpu-index "${EXPECTED_GPU_INDEX}" \
    --gpu-uuid "${EXPECTED_GPU_UUID}" \
    --service-uid "${SERVICE_UID}" \
    --model "$(config_value "${CONFIG}" VLLM_MODEL)" \
    --served-model-name "$(config_value "${CONFIG}" VLLM_SERVED_MODEL_NAME)" \
    --model-revision "$(config_value "${CONFIG}" VLLM_MODEL_REVISION)"
}

verify_cell_boundary() {
  verify_locked_identity
  verify_active_service
  verify_locked_identity
  verify_admission
  verify_network_inventory "$1"
  verify_caddy
  verify_dumpcap_access || die "dumpcap capture identity drifted"
  verify_locked_identity
}

run_cell() {
  local network="$1" workload="$2" transport="$3" samples manifest manifest_sha cell
  local filter secure_port child_status
  if [[ "${workload}" == qa ]]; then
    samples=32
    manifest="${EXPERIMENT_ROOT}/$(config_value "${CONFIG}" MANIFEST_PATH)"
    manifest_sha="$(config_value "${CONFIG}" MANIFEST_SHA256)"
  else
    samples=20
    manifest="${EXPERIMENT_ROOT}/$(config_value "${CONFIG}" SUMMARY_MANIFEST_PATH)"
    manifest_sha="$(config_value "${CONFIG}" SUMMARY_MANIFEST_SHA256)"
  fi
  cell="${MATRIX_ROOT}/cells/${network}/${workload}/${transport}"
  if [[ -f "${cell}/CELL_COMPLETE.json" ]]; then
    printf 'SKIP complete cell %s/%s/%s\n' "${network}" "${workload}" "${transport}"
    return 0
  fi
  [[ ! -e "${cell}" && ! -L "${cell}" ]] ||
    [[ -d "${cell}" && ! -L "${cell}" &&
      "$(/usr/bin/stat -c %u:%g -- "${cell}")" == "${SERVICE_UID}:${SERVICE_GID}" ]] ||
    die "unsafe resumable cell directory: ${cell}"
  /usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 \
    "${MATRIX_ROOT}/cells" "${MATRIX_ROOT}/cells/${network}" \
    "${MATRIX_ROOT}/cells/${network}/${workload}"
  /usr/bin/install -d -o "${SERVICE_UID}" -g "${SERVICE_GID}" -m 0700 "${cell}"
  if [[ "${transport}" == tls13 ]]; then
    filter=tcp
    secure_port=8443
  else
    filter=udp
    secure_port=8444
  fi
  verify_cell_boundary "${network}"
  /usr/sbin/ip netns exec llm-client /usr/bin/bash -p -c '
      exec 8>&-
      exec /usr/bin/setpriv --reuid "$1" --regid "$2" --groups "${18}" \
        /usr/bin/env -i HOME=/tmp LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC \
        PATH=/usr/sbin:/usr/bin PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
        PYTHONSAFEPATH=1 PYTHONPATH="$3" \
        "$4" -P "$5" --controller-pid "$6" --controller-start-ticks "$7" -- run \
        --manifest "$8" --output-dir "$9" \
        --backend local_vllm --base-url "https://10.200.0.1:${16}/v1" \
        --model "${10}" --samples "${11}" --repetitions 3 --seed 42 \
        --max-output-tokens 4096 --request-timeout-seconds 900 \
        --observation-seconds 900 --capture-interface llmclient0 \
        --capture-filter "${12} port ${16}" --capture-stop-on-response \
        --worker-count 1 --worker-index 0 --worker-gpu-index "${17}" \
        --worker-gpu-uuid "${13}" --topology-worker-index 0 \
        --transport "${14}" --connection-mode warm --tls-ca-file "${15}"
    ' matrix-child "${SERVICE_UID}" "${SERVICE_GID}" "${REPOSITORY_ROOT}" \
      "${RUNNER_PYTHON}" "${REQUEST_TOOL}" "${CONTROLLER_PID}" "${CONTROLLER_TICKS}" \
      "${manifest}" "${cell}" \
      "$(config_value "${CONFIG}" VLLM_SERVED_MODEL_NAME)" "${samples}" \
      "${filter}" "${EXPECTED_GPU_UUID}" "${transport}" "${CA_FILE}" \
      "${secure_port}" "${EXPECTED_GPU_INDEX}" "${DUMPCAP_GID}" ||
    child_status=$?
  [[ "${child_status:-0}" -eq 0 ]] ||
    die "measurement cell failed; append-only attempts remain in ${cell}"
  verify_cell_boundary "${network}"
  /usr/bin/python3 -I "${STATE_TOOL}" seal-cell --root "${MATRIX_ROOT}" \
    --network "${network}" --workload "${workload}" --transport "${transport}" \
    --manifest-sha256 "${manifest_sha}" --service-uid "${SERVICE_UID}" \
    --gpu-index "${EXPECTED_GPU_INDEX}" \
    --gpu-uuid "${EXPECTED_GPU_UUID}"
  verify_cell_boundary "${network}"
}

release_precheck
load_policy
check_base_output_hierarchy
acquire_service_lock
pin_service_state
select_gpu_output_hierarchy
verify_active_service
verify_locked_identity
verify_admission
verify_locked_identity
MATRIX_ROOT="${OUTPUT_ROOT}/runs/${RUN_ID}"
if [[ ! -e "${MATRIX_ROOT}" && ! -L "${MATRIX_ROOT}" ]]; then
  if [[ "${ACTION}" == check ]]; then
    check_idle_resources
    printf 'PRIVILEGED_MATRIX_PRECHECK_OK repository_sha=%s gpu_uuid=%s expected_calls=2808\n' \
      "${REPOSITORY_SHA}" "${EXPECTED_GPU_UUID}"
    exit 0
  fi
  if [[ "${ACTION}" == status ]]; then
    printf '{"completed_cells":[],"state":"not-started","total_cells":12}\n'
    exit 0
  fi
  [[ "${ACTION}" == run ]] || die "fresh matrix root is absent; use run, not resume"
  /usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${MATRIX_ROOT}"
fi
[[ -d "${MATRIX_ROOT}" && ! -L "${MATRIX_ROOT}" &&
  "$(/usr/bin/stat -c %u:%g:%a -- "${MATRIX_ROOT}")" == "0:${SERVICE_GID}:710" ]] ||
  die "unsafe matrix root"

if [[ "${ACTION}" == check ]]; then
  check_idle_resources
  printf 'PRIVILEGED_MATRIX_PRECHECK_OK repository_sha=%s gpu_uuid=%s expected_calls=2808\n' \
    "${REPOSITORY_SHA}" "${EXPECTED_GPU_UUID}"
  exit 0
fi
mapfile -d '' -t PLAN_ARGS < <(state_args)
if [[ "${ACTION}" == run ]]; then
  [[ ! -e "${MATRIX_ROOT}/MATRIX_COMPLETE.json" ]] || die "matrix is already complete"
  /usr/bin/python3 -I "${STATE_TOOL}" create-plan "${PLAN_ARGS[@]}"
else
  /usr/bin/python3 -I "${STATE_TOOL}" verify-plan "${PLAN_ARGS[@]}"
fi
/usr/bin/python3 -I "${STATE_TOOL}" status --root "${MATRIX_ROOT}" >/dev/null
if [[ "${ACTION}" == status ]]; then
  /usr/bin/python3 -I "${STATE_TOOL}" status --root "${MATRIX_ROOT}"
  exit 0
fi
[[ "${ACTION}" == run || "${ACTION}" == resume ]] || die "invalid matrix action"
[[ ! -e "${MATRIX_ROOT}/MATRIX_COMPLETE.json" ]] ||
  die "matrix is already immutably complete"

trap matrix_cleanup EXIT INT TERM HUP
for network in baseline rtt realistic; do
  check_idle_resources
  apply_network "${network}"
  start_caddy
  for workload in qa summary; do
    for transport in tls13 http3; do
      run_cell "${network}" "${workload}" "${transport}"
    done
  done
  stop_caddy
  cleanup_network
  verify_active_service
  verify_locked_identity
done
/usr/bin/python3 -I "${STATE_TOOL}" seal-matrix --root "${MATRIX_ROOT}"
verify_active_service
verify_locked_identity
verify_admission
trap - EXIT INT TERM HUP
cleanup 0 return || exit $?
printf 'PRIVILEGED_MATRIX_COMPLETE root=%s expected_calls=2808\n' "${MATRIX_ROOT}"
