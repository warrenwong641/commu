#!/usr/bin/bash -p
set -euo pipefail
set +x
umask 077

# Start one bounded matrix lease segment from an installed, root-owned release.
# GPU identity belongs to the immutable run plan; lease duration does not.

FIXED_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
MANAGER=/usr/local/sbin/commu-vllm-lease-manager
HELPER=/usr/local/libexec/commu-vllm-service-lease
MANAGER_SHA256=93b6123c3ced3dbe028db80848d98da7ccb1fd465950a3464b56c67c6d371760
HELPER_SHA256=613eb7e98d7cf03871923f44a532d45b2592b9e3f10ef5ebec9fa40e29f611de
RECORDS_ROOT=/var/lib/commu-matrix-segments
SERVICE_RUNTIME_NAMESPACE=/var/lib/commu-matrix-runtime

CLEAN_ENV_DECLARATION="$(declare -p COMMU_MATRIX_SEGMENT_CLEAN_ENV 2>/dev/null || true)"
if [[ "${CLEAN_ENV_DECLARATION}" != 'declare -r COMMU_MATRIX_SEGMENT_CLEAN_ENV="1"' ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'ERROR: launcher must run as root\n' >&2; exit 2; }
  SELF="$(/usr/bin/readlink -e -- "${BASH_SOURCE[0]}")" || exit 2
  [[ -f "${SELF}" && ! -L "${SELF}" && "$(/usr/bin/stat -c %u -- "${SELF}")" == 0 ]] || exit 2
  exec /usr/bin/env -i \
    HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
    /usr/bin/bash -p -c '
      readonly COMMU_MATRIX_SEGMENT_CLEAN_ENV=1
      launcher="$1"
      shift
      source "${launcher}" "$@"
    ' commu-matrix-clean-env "${SELF}" "$@"
fi
PATH="${FIXED_PATH}"
export PATH HOME LANG LC_ALL TZ PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE PYTHONSAFEPATH
[[ "$(declare -p COMMU_MATRIX_SEGMENT_CLEAN_ENV 2>/dev/null || true)" == 'declare -r COMMU_MATRIX_SEGMENT_CLEAN_ENV="1"' ]] || {
  printf 'ERROR: clean-environment shell marker drifted\n' >&2
  exit 2
}
while IFS='=' read -r inherited_name _; do
  case "${inherited_name}" in
    HOME|LANG|LC_ALL|TZ|PATH|PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|PYTHONSAFEPATH|PWD|SHLVL|_) ;;
    *) printf 'ERROR: unsanitized environment variable: %s\n' "${inherited_name}" >&2; exit 2 ;;
  esac
done < <(/usr/bin/env)
[[ "${EUID}" -eq 0 ]] || { printf 'ERROR: launcher must run as root\n' >&2; exit 2; }
cd /
unset OLDPWD

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
usage() {
  cat >&2 <<'EOF'
Usage:
  32_launch_privileged_matrix_segment.sh new \
    --run-id SAFE_ID --gpu-index N --lease 20|115m|2h \
    [--service-config-template POLICY_SCOPED_PATH]
  32_launch_privileged_matrix_segment.sh resume \
    --run-id SAFE_ID --lease 20|115m|2h \
    [--gpu-index N] [--source-repository-sha 40_HEX] \
    [--authorization-cutoff-epoch EPOCH] \
    [--service-config-template POLICY_SCOPED_PATH]
  32_launch_privileged_matrix_segment.sh continue-on-gpu \
    --parent-run-id SAFE_ID --parent-run-repository-sha 40_HEX \
    --run-id NEW_SAFE_ID --gpu-index N --lease 20|115m|2h \
    [--authorization-cutoff-epoch EPOCH] \
    [--bootstrap-admission-if-missing] \
    [--service-config-template POLICY_SCOPED_PATH]

For resume, --gpu-index is an assertion only. The immutable RUN_PLAN.json
selects the GPU. A new run must use a new run ID.
continue-on-gpu is the only cross-GPU path: it creates a separate target root,
snapshots the immutable parent ledger, and never writes the parent run.
--parent-source-repository-sha remains a compatibility alias for
--parent-run-repository-sha.  The launcher derives the frozen service/pilot
source from the immutable parent plan instead of trusting a caller override.
EOF
  exit 2
}
sha256_file() { /usr/bin/sha256sum -- "$1" | /usr/bin/awk '{print $1}'; }
kv_value() {
  /usr/bin/awk -F= -v key="$1" '
    $1 == key {sub(/^[^=]*=/, ""); value=$0; count++}
    END {if (count != 1) exit 1; print value}
  ' "$2"
}
regular_root_file() {
  local path="$1" mode
  [[ -f "${path}" && ! -L "${path}" && "$(/usr/bin/stat -c %u:%h -- "${path}")" == 0:1 ]] || return 1
  mode="$(/usr/bin/stat -c %a -- "${path}")" || return 1
  (( (8#${mode} & 8#022) == 0 ))
}
trusted_root_dir() {
  local path="$1" mode
  [[ -d "${path}" && ! -L "${path}" && "$(/usr/bin/readlink -e -- "${path}")" == "${path}" &&
    "$(/usr/bin/stat -c %u:%g -- "${path}")" == 0:0 ]] || return 1
  mode="$(/usr/bin/stat -c %a -- "${path}")" || return 1
  (( (8#${mode} & 8#022) == 0 ))
}

SELF="$(/usr/bin/readlink -e -- "${BASH_SOURCE[0]}")" || die "cannot resolve launcher"
SCRIPT_DIR="$(/usr/bin/dirname -- "${SELF}")"
EXPERIMENT_ROOT="$(/usr/bin/readlink -e -- "${SCRIPT_DIR}/..")" || die "cannot resolve experiment root"
REPOSITORY_ROOT="$(/usr/bin/readlink -e -- "${EXPERIMENT_ROOT}/..")" || die "cannot resolve release repository"
RELEASE_ROOT="$(/usr/bin/readlink -e -- "${REPOSITORY_ROOT}/..")" || die "cannot resolve release root"
METADATA="${RELEASE_ROOT}/RELEASE_METADATA"
MANIFEST="${RELEASE_ROOT}/RELEASE_FILES.sha256"
RUNTIME_MANIFEST="${RELEASE_ROOT}/INSTALLED_RUNTIME_FILES.sha256"
POLICY="${RELEASE_ROOT}/policy/service.state"
RUNNER="${SCRIPT_DIR}/31_run_privileged_matrix.sh"
LAUNCH_HELPER="${SCRIPT_DIR}/privileged_matrix_launch.py"
STATE_TOOL="${SCRIPT_DIR}/privileged_matrix_state.py"

release_precheck() {
  local path
  case "${RELEASE_ROOT}" in /opt/commu-secure-matrix/releases/[0-9a-f][0-9a-f]*) ;; *) die "launcher is outside the fixed release root" ;; esac
  for path in /opt /opt/commu-secure-matrix /opt/commu-secure-matrix/releases \
    "${RELEASE_ROOT}" "${REPOSITORY_ROOT}" "${EXPERIMENT_ROOT}" "${SCRIPT_DIR}"; do
    trusted_root_dir "${path}" || die "unsafe release directory: ${path}"
  done
  for path in "${SELF}" "${METADATA}" "${MANIFEST}" "${RUNTIME_MANIFEST}" "${POLICY}" \
    "${RUNNER}" "${LAUNCH_HELPER}" "${STATE_TOOL}"; do
    regular_root_file "${path}" || die "unsafe release file: ${path}"
  done
  (
    cd "${RELEASE_ROOT}"
    /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
    /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
  ) || die "release digest verification failed"
  REPOSITORY_SHA="$(kv_value repository_sha "${METADATA}")" || die "missing release SHA"
  [[ "${REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ && "$(/usr/bin/basename -- "${RELEASE_ROOT}")" == "${REPOSITORY_SHA}" ]] ||
    die "release identity mismatch"
  [[ "$(kv_value schema "${POLICY}")" == commu-privileged-matrix-policy-v2 &&
    "$(kv_value repository_sha "${POLICY}")" == "${REPOSITORY_SHA}" ]] || die "wrong release policy"
  SERVICE_STATE_ROOT="$(kv_value service_state_root "${POLICY}")" || die "missing service-state root"
  SERVICE_USER="$(kv_value service_user "${POLICY}")" || die "missing service user"
  SERVICE_UID="$(kv_value service_uid "${POLICY}")" || die "missing service UID"
  SERVICE_GID="$(kv_value service_gid "${POLICY}")" || die "missing service GID"
  PILOT_SHA="$(kv_value pilot_repository_sha "${POLICY}")" || die "missing pilot repository SHA"
  [[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*$ && "${SERVICE_UID}" =~ ^[1-9][0-9]*$ &&
    "${SERVICE_GID}" =~ ^[1-9][0-9]*$ && "${PILOT_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "unsafe policy identity"
  [[ "$(/usr/bin/id -u "${SERVICE_USER}")" == "${SERVICE_UID}" && "$(/usr/bin/id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] ||
    die "service account identity drifted"
  [[ "${SERVICE_STATE_ROOT}" = /* && -d "${SERVICE_STATE_ROOT}" && ! -L "${SERVICE_STATE_ROOT}" &&
    "$(/usr/bin/stat -c %u -- "${SERVICE_STATE_ROOT}")" == "${SERVICE_UID}" ]] || die "unsafe service-state root"
  printf '%s  %s\n' "${HELPER_SHA256}" "${HELPER}" "${MANAGER_SHA256}" "${MANAGER}" |
    /usr/bin/sha256sum --check --strict --quiet || die "installed lease helpers differ from the reviewed versions"

  EXPECTED_API_VLLM="$(kv_value EXPECTED_API_VLLM "${RUNNER}")" || die "runner has no unique API runtime"
  EXPECTED_API_LD_LIBRARY_PATH="$(kv_value EXPECTED_API_LD_LIBRARY_PATH "${RUNNER}")" || die "runner has no unique API library path"
  EXPECTED_CONTROLLER_LAUNCHER="$(kv_value EXPECTED_CONTROLLER_LAUNCHER "${RUNNER}")" || die "runner has no unique service launcher"
  [[ "${EXPECTED_API_VLLM}" = /* && "${EXPECTED_API_LD_LIBRARY_PATH}" = /* &&
    "${EXPECTED_CONTROLLER_LAUNCHER}" = /*/traffic_experiment/scripts/03_start_vllm_dual.sh ]] ||
    die "runner runtime policy is malformed"
}

verify_measurement_source() {
  local source_sha="$1" source_matrix source_pilot path
  source_matrix="/opt/commu-secure-matrix/releases/${source_sha}"
  source_pilot="/opt/commu-protocol-pilots/releases/${source_sha}"
  for path in "${source_matrix}" "${source_matrix}/repository" "${source_matrix}/repository/traffic_experiment" \
    "${source_pilot}" "${source_pilot}/repository" "${source_pilot}/repository/traffic_experiment"; do
    trusted_root_dir "${path}" || die "unsafe or missing source release: ${path}"
  done
  for path in "${source_matrix}/RELEASE_METADATA" "${source_matrix}/RELEASE_FILES.sha256" \
    "${source_matrix}/INSTALLED_RUNTIME_FILES.sha256" "${source_matrix}/policy/service.state" "${source_matrix}/config/server.env" \
    "${source_pilot}/RELEASE_METADATA" "${source_pilot}/RELEASE_FILES.sha256" \
    "${source_pilot}/INSTALLED_RUNTIME_FILES.sha256" "${source_pilot}/policy/service.state"; do
    regular_root_file "${path}" || die "unsafe source-release anchor: ${path}"
  done
  (
    cd "${source_matrix}"
    /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
    /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
  ) || die "source matrix release digest verification failed"
  (
    cd "${source_pilot}"
    /usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256
    /usr/bin/sha256sum --check --strict --quiet INSTALLED_RUNTIME_FILES.sha256
  ) || die "source pilot release digest verification failed"
  [[ "$(kv_value repository_sha "${source_matrix}/RELEASE_METADATA")" == "${source_sha}" &&
    "$(kv_value repository_sha "${source_matrix}/policy/service.state")" == "${source_sha}" &&
    "$(kv_value pilot_repository_sha "${source_matrix}/policy/service.state")" == "${source_sha}" &&
    "$(kv_value repository_sha "${source_pilot}/RELEASE_METADATA")" == "${source_sha}" &&
    "$(kv_value repository_sha "${source_pilot}/policy/service.state")" == "${source_sha}" ]] ||
    die "source matrix/pilot release identity mismatch"
  SOURCE_PILOT_RUNNER="${source_pilot}/repository/traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh"
  regular_root_file "${SOURCE_PILOT_RUNNER}" || die "unsafe source pilot runner"
  MEASUREMENT_RELEASE_ROOT="${source_matrix}"
  MEASUREMENT_CONFIG="${source_matrix}/config/server.env"
  MEASUREMENT_CONFIG_SHA256="$(sha256_file "${MEASUREMENT_CONFIG}")"
}

find_resume_plan() {
  local -a candidates=() helper_args=() plan_fields=()
  local candidate parent identity_json
  while IFS= read -r -d '' candidate; do
    parent="$(/usr/bin/basename -- "$(/usr/bin/dirname -- "${candidate}")")"
    [[ "${parent}" == "${RUN_ID}" ]] || continue
    if [[ -n "${GPU_INDEX_ASSERTION}" ]]; then
      case "${candidate}" in /var/lib/commu-secure-matrix/[0-9a-f]*/gpu-"${GPU_INDEX_ASSERTION}"-GPU-*/runs/"${RUN_ID}"/RUN_PLAN.json) ;; *) continue ;; esac
    fi
    if [[ -n "${SOURCE_REPOSITORY_SHA}" ]]; then
      case "${candidate}" in "/var/lib/commu-secure-matrix/${SOURCE_REPOSITORY_SHA}/"*) ;; *) continue ;; esac
    fi
    candidates+=("${candidate}")
  done < <(/usr/bin/find -P /var/lib/commu-secure-matrix -mindepth 5 -maxdepth 5 -type f -name RUN_PLAN.json -print0)
  [[ "${#candidates[@]}" -eq 1 ]] || die "resume requires exactly one immutable RUN_PLAN.json for run ID ${RUN_ID}; found ${#candidates[@]}"
  PLAN="${candidates[0]}"
  regular_root_file "${PLAN}" && [[ "$(/usr/bin/stat -c %a -- "${PLAN}")" == 444 ]] ||
    die "RUN_PLAN.json is not immutable root-owned release state"
  helper_args=(resume-identity --plan "${PLAN}")
  [[ -z "${GPU_INDEX_ASSERTION}" ]] || helper_args+=(--gpu-index "${GPU_INDEX_ASSERTION}")
  identity_json="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" "${helper_args[@]}")" || die "could not read immutable run identity"
  mapfile -d '' -t plan_fields < <(/usr/bin/python3 -I - "${identity_json}" <<'PY'
import json, sys
identity = json.loads(sys.argv[1])
for name in ("repository_sha", "gpu_index", "gpu_uuid", "active_config_sha256", "run_plan_sha256"):
    sys.stdout.buffer.write(str(identity[name]).encode("ascii") + b"\0")
for name in ("parent_matrix_root", "parent_repository_sha", "service_repository_sha"):
    value = identity[name]
    sys.stdout.buffer.write(("" if value is None else str(value)).encode("ascii") + b"\0")
PY
  )
  [[ "${#plan_fields[@]}" -eq 8 ]] || die "incomplete run-plan identity"
  RUN_REPOSITORY_SHA="${plan_fields[0]}"
  GPU_INDEX="${plan_fields[1]}"
  GPU_UUID="${plan_fields[2]}"
  PLAN_ACTIVE_CONFIG_SHA256="${plan_fields[3]}"
  PLAN_SHA256="${plan_fields[4]}"
  PARENT_MATRIX_ROOT="${plan_fields[5]}"
  PARENT_RUN_REPOSITORY_SHA="${plan_fields[6]}"
  SERVICE_REPOSITORY_SHA="${plan_fields[7]}"
  [[ -z "${SOURCE_REPOSITORY_SHA}" || "${SOURCE_REPOSITORY_SHA}" == "${RUN_REPOSITORY_SHA}" ]] ||
    die "source repository assertion differs from RUN_PLAN.json"
}

find_parent_plan() {
  local -a candidates=() parent_fields=()
  local candidate parent identity_json
  while IFS= read -r -d '' candidate; do
    parent="$(/usr/bin/basename -- "$(/usr/bin/dirname -- "${candidate}")")"
    [[ "${parent}" == "${PARENT_RUN_ID}" ]] || continue
    case "${candidate}" in
      "/var/lib/commu-secure-matrix/${PARENT_RUN_REPOSITORY_SHA}/"*/runs/"${PARENT_RUN_ID}"/RUN_PLAN.json) ;;
      *) continue ;;
    esac
    candidates+=("${candidate}")
  done < <(/usr/bin/find -P /var/lib/commu-secure-matrix -mindepth 5 -maxdepth 5 -type f -name RUN_PLAN.json -print0)
  [[ "${#candidates[@]}" -eq 1 ]] ||
    die "continuation requires exactly one immutable parent RUN_PLAN.json; found ${#candidates[@]}"
  PARENT_PLAN="${candidates[0]}"
  regular_root_file "${PARENT_PLAN}" && [[ "$(/usr/bin/stat -c %a -- "${PARENT_PLAN}")" == 444 ]] ||
    die "parent RUN_PLAN.json is not immutable root-owned release state"
  identity_json="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" resume-identity --plan "${PARENT_PLAN}")" ||
    die "could not read immutable parent identity"
  mapfile -d '' -t parent_fields < <(/usr/bin/python3 -I - "${identity_json}" <<'PY'
import json, sys
identity = json.loads(sys.argv[1])
for name in ("repository_sha", "run_id", "gpu_index", "gpu_uuid", "service_repository_sha"):
    sys.stdout.buffer.write(str(identity[name]).encode("ascii") + b"\0")
PY
  )
  [[ "${#parent_fields[@]}" -eq 5 && "${parent_fields[0]}" == "${PARENT_RUN_REPOSITORY_SHA}" &&
    "${parent_fields[1]}" == "${PARENT_RUN_ID}" ]] || die "parent run/source assertion differs from RUN_PLAN.json"
  PARENT_GPU_INDEX="${parent_fields[2]}"
  PARENT_GPU_UUID="${parent_fields[3]}"
  SERVICE_REPOSITORY_SHA="${parent_fields[4]}"
  PARENT_MATRIX_ROOT="$(/usr/bin/dirname -- "${PARENT_PLAN}")"
}

resolve_gpu() {
  local rows
  rows="$(/usr/bin/nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits)" || die "cannot query GPU inventory"
  GPU_UUID="$(/usr/bin/awk -F, -v wanted="${GPU_INDEX}" '
    {gsub(/[[:space:]]/, "", $1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); if ($1 == wanted) {print $2; found++}}
    END {if (found != 1) exit 1}
  ' <<<"${rows}")" || die "GPU index ${GPU_INDEX} has no unique UUID"
  [[ "${GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ ]] || die "GPU inventory returned an unsafe UUID"
}

validate_service_template() {
  local requested="$1" resolved config_revision config_mode
  resolved="$(/usr/bin/readlink -e -- "${requested}")" || die "service config does not exist: ${requested}"
  case "${resolved}" in
    "${SERVICE_STATE_ROOT}"/qwen35-"${SERVICE_REPOSITORY_SHA:0:7}"/server.gpu[0-9]*.single.env) ;;
    *) die "service template must be a single-GPU config below the source-SHA policy directory" ;;
  esac
  [[ "$(/usr/bin/basename -- "${resolved}")" =~ ^server\.gpu(0|[1-9][0-9]*)\.single\.env$ ]] ||
    die "service template filename is not canonical"
  config_mode="$(/usr/bin/stat -c %a -- "${resolved}")" || die "cannot inspect service config mode"
  [[ -f "${resolved}" && ! -L "${resolved}" && "$(/usr/bin/stat -c %u:%g:%h -- "${resolved}")" == "${SERVICE_UID}:${SERVICE_GID}:1" &&
    ( "${config_mode}" == 400 || "${config_mode}" == 600 ) ]] ||
    die "service config must be singly linked, mode 0400/0600, and service-user-owned"
  config_revision="$(/usr/bin/awk -F= '$1 == "VLLM_MODEL_REVISION" {gsub(/\"/, "", $2); print $2; count++} END {if(count != 1) exit 1}' "${resolved}")" ||
    die "service config has no unique model revision"
  [[ "${config_revision}" =~ ^[0-9a-f]{40}$ ]] || die "service config model revision is not pinned"
  SERVICE_TEMPLATE="${resolved}"
}

record_value() { kv_value "$1" "${RECORD}"; }
load_record() {
  local id="$1"
  [[ "${id}" =~ ^[0-9a-f]{16}$ ]] || die "invalid internal record ID"
  [[ -d "${RECORDS_ROOT}" && ! -L "${RECORDS_ROOT}" &&
    "$(/usr/bin/stat -c %u:%g:%a -- "${RECORDS_ROOT}")" == "0:${SERVICE_GID}:710" ]] ||
    die "unsafe segment records root"
  RECORD_DIR="${RECORDS_ROOT}/${id}"
  RECORD="${RECORD_DIR}/record.state"
  [[ -d "${RECORD_DIR}" && ! -L "${RECORD_DIR}" && "$(/usr/bin/stat -c %u:%g:%a -- "${RECORD_DIR}")" == "0:${SERVICE_GID}:710" ]] ||
    die "unsafe segment record directory"
  [[ -f "${RECORD}" && ! -L "${RECORD}" && "$(/usr/bin/stat -c %u:%g:%a:%h -- "${RECORD}")" == 0:0:600:1 ]] ||
    die "unsafe segment record"
  CLEANUP_LOCK="${RECORD_DIR}/cleanup.lock"
  [[ -f "${CLEANUP_LOCK}" && ! -L "${CLEANUP_LOCK}" && "$(/usr/bin/stat -c %u:%g:%a:%h -- "${CLEANUP_LOCK}")" == 0:0:600:1 ]] ||
    die "unsafe segment cleanup lock"
  RECORD_ID="$(record_value record_id)"; SESSION="$(record_value session)"; TIMER_BASE="$(record_value timer_base)"
  STATE="$(record_value service_state)"; SERVICE_CONFIG="$(record_value service_config)"
  VLLM_LOG="$(record_value vllm_log)"; MATRIX_LOG="$(record_value matrix_log)"
  [[ "${MATRIX_LOG}" == "${RECORD_DIR}/matrix.log" && -f "${MATRIX_LOG}" && ! -L "${MATRIX_LOG}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${MATRIX_LOG}")" == "0:${SERVICE_GID}:640:1" ]] ||
    die "unsafe matrix log"
  EXPIRY_LOG="${RECORD_DIR}/expiry.log"
  [[ -f "${EXPIRY_LOG}" && ! -L "${EXPIRY_LOG}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${EXPIRY_LOG}")" == "0:${SERVICE_GID}:640:1" ]] ||
    die "unsafe expiry log"
  RUN_ID="$(record_value run_id)"; MODE="$(record_value mode)"
  GPU_INDEX="$(record_value gpu_index)"; GPU_UUID="$(record_value gpu_uuid)"
  RUN_REPOSITORY_SHA="$(record_value run_repository_sha)"
  SERVICE_REPOSITORY_SHA="$(record_value service_repository_sha)"
  PARENT_RUN_REPOSITORY_SHA="$(record_value parent_run_repository_sha)"
  MEASUREMENT_REPOSITORY_SHA="$(record_value measurement_repository_sha)"
  PARENT_MATRIX_ROOT="$(record_value parent_matrix_root)"
  BOOTSTRAP_ADMISSION="$(record_value bootstrap_admission)"
  SOURCE_PILOT_RUNNER="$(record_value source_pilot_runner)"
  PILOT_UNIT="$(record_value pilot_unit)"
  MEASUREMENT_CONFIG="$(record_value measurement_config)"
  HARD_DEADLINE_EPOCH="$(record_value hard_deadline_epoch)"
  SERVICE_REPOSITORY_ROOT="$(record_value service_repository_root)"
  MATRIX_ROOT="/var/lib/commu-secure-matrix/${RUN_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/runs/${RUN_ID}"
  [[ "${RECORD_ID}" == "${id}" && "${SESSION}" == "commu-matrix-${id}" && "${TIMER_BASE}" == "${SESSION}-expiry" &&
    "${RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ && "${MODE}" =~ ^(new|resume|continue-on-gpu)$ &&
    "${GPU_INDEX}" =~ ^(0|[1-9][0-9]*)$ && "${GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ &&
    "${RUN_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ && "${SERVICE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ &&
    "${MEASUREMENT_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ &&
    ( "${PARENT_RUN_REPOSITORY_SHA}" == none || "${PARENT_RUN_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ) &&
    "${BOOTSTRAP_ADMISSION}" =~ ^[01]$ &&
    "${HARD_DEADLINE_EPOCH}" =~ ^[0-9]+$ && "${MEASUREMENT_CONFIG}" == "/opt/commu-secure-matrix/releases/${MEASUREMENT_REPOSITORY_SHA}/config/server.env" &&
    "${MATRIX_ROOT}" == "/var/lib/commu-secure-matrix/${RUN_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/runs/${RUN_ID}" ]] ||
    die "segment record contains unsafe values"
  if [[ "${PARENT_MATRIX_ROOT}" == none ]]; then
    [[ "${MODE}" != continue-on-gpu && "${PARENT_RUN_REPOSITORY_SHA}" == none ]] ||
      die "continuation record has no parent"
  else
    case "${PARENT_MATRIX_ROOT}" in
      "/var/lib/commu-secure-matrix/${PARENT_RUN_REPOSITORY_SHA}"/gpu-[0-9]*-GPU-*/runs/*) ;;
      *) die "segment record has an unsafe parent matrix root" ;;
    esac
    [[ "${MODE}" != new && "${RUN_REPOSITORY_SHA}" == "${REPOSITORY_SHA}" &&
      "${MEASUREMENT_REPOSITORY_SHA}" == "${REPOSITORY_SHA}" ]] ||
      die "continuation segment release identities are inconsistent"
  fi
  if [[ "${BOOTSTRAP_ADMISSION}" == 1 ]]; then
    [[ "${MODE}" == continue-on-gpu &&
      "${SOURCE_PILOT_RUNNER}" == "/opt/commu-protocol-pilots/releases/${SERVICE_REPOSITORY_SHA}/repository/traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh" &&
      "${PILOT_UNIT}" == "commu-matrix-${RECORD_ID}-pilot.service" ]] ||
      die "segment record has an unsafe pilot-bootstrap identity"
    regular_root_file "${SOURCE_PILOT_RUNNER}" || die "segment pilot runner is unsafe"
  else
    [[ "${SOURCE_PILOT_RUNNER}" == none && "${PILOT_UNIT}" == none ]] ||
      die "unexpected pilot runner in segment record"
  fi
}

pilot_main() {
  local id="$1"
  load_record "${id}"
  [[ "${BOOTSTRAP_ADMISSION}" == 1 ]] || die "pilot worker record does not request admission bootstrap"
  "${SOURCE_PILOT_RUNNER}" check --service-state "${STATE}"
  "${SOURCE_PILOT_RUNNER}" run --service-state "${STATE}"
  "${SOURCE_PILOT_RUNNER}" admission --service-state "${STATE}"
}

stop_service() {
  [[ -f "${STATE}" && ! -L "${STATE}" ]] || return 0
  [[ "$(kv_value status "${STATE}" 2>/dev/null || true)" == running ]] || return 0
  "${MANAGER}" stop --service-user "${SERVICE_USER}" --helper "${HELPER}" \
    --state "${STATE}" --repository-root "${SERVICE_REPOSITORY_ROOT}" --timeout 300
}

proc_tail() {
  local line
  [[ "$1" =~ ^[0-9]+$ && -r "/proc/$1/stat" ]] || return 1
  IFS= read -r line <"/proc/$1/stat" || return 1
  [[ "${line}" == *") "* ]] || return 1
  printf '%s\n' "${line##*) }"
}
proc_field() { local tail; tail="$(proc_tail "$1")" || return 1; /usr/bin/awk -v n="$2" '{print $n}' <<<"${tail}"; }
proc_state() { proc_field "$1" 1; }
proc_ticks() { proc_field "$1" 20; }
proc_uid() { /usr/bin/awk '/^Uid:/ {print $2; exit}' "/proc/$1/status" 2>/dev/null; }
proc_exe() { /usr/bin/readlink -e -- "/proc/$1/exe" 2>/dev/null; }
proc_args() {
  local -n output="$2"
  output=()
  while IFS= read -r -d '' argument; do output+=("${argument}"); done <"/proc/$1/cmdline"
}
tcp_port_closed() {
  local listeners
  listeners="$(/usr/bin/ss -H -ltn "sport = :$1" 2>/dev/null)" || return 1
  [[ -z "${listeners}" ]]
}
udp_port_closed() {
  local listeners
  listeners="$(/usr/bin/ss -H -lun "sport = :$1" 2>/dev/null)" || return 1
  [[ -z "${listeners}" ]]
}

stop_caddy_exact() {
  local state="$1" expected_caddy expected_config schema pid ticks exe config live_ticks live_state
  local -a args=() expected=()
  expected_caddy="/opt/commu-secure-matrix/releases/${MEASUREMENT_REPOSITORY_SHA}/repository/traffic_experiment/.tools/caddy"
  expected_config="/opt/commu-secure-matrix/releases/${MEASUREMENT_REPOSITORY_SHA}/repository/traffic_experiment/configs/Caddyfile.single"
  if [[ ! -e "${state}" && ! -L "${state}" ]]; then
    tcp_port_closed 8443 && udp_port_closed 8444 && tcp_port_closed 8543 && udp_port_closed 8544
    return
  fi
  [[ -f "${state}" && ! -L "${state}" && "$(/usr/bin/stat -c %u:%a:%h -- "${state}")" == 0:400:1 ]] ||
    die "Caddy fallback state is unsafe"
  schema="$(kv_value schema "${state}")"; pid="$(kv_value pid "${state}")"; ticks="$(kv_value start_ticks "${state}")"
  exe="$(kv_value exe "${state}")"; config="$(kv_value config "${state}")"
  [[ "${schema}" == commu-secure-matrix-caddy-v1 && "${pid}" =~ ^[1-9][0-9]*$ &&
    "${ticks}" =~ ^[1-9][0-9]*$ && "${exe}" == "${expected_caddy}" && "${config}" == "${expected_config}" ]] ||
    die "Caddy fallback identity record differs from the source release"
  regular_root_file "${expected_caddy}" && regular_root_file "${expected_config}" ||
    die "source-release Caddy files are unsafe"
  if [[ ! -r "/proc/${pid}/stat" ]]; then
    tcp_port_closed 8443 && udp_port_closed 8444 && tcp_port_closed 8543 && udp_port_closed 8544 ||
      die "Caddy process is gone but protected listeners remain"
    /usr/bin/rm -- "${state}"
    return
  fi
  live_ticks="$(proc_ticks "${pid}" 2>/dev/null || true)"; live_state="$(proc_state "${pid}" 2>/dev/null || true)"
  if [[ "${live_ticks}" != "${ticks}" || "${live_state}" == Z ]]; then
    tcp_port_closed 8443 && udp_port_closed 8444 && tcp_port_closed 8543 && udp_port_closed 8544 ||
      die "recorded Caddy exited but protected listeners remain"
    /usr/bin/rm -- "${state}"
    return
  fi
  proc_args "${pid}" args || die "cannot read recorded Caddy arguments"
  expected=("${expected_caddy}" run --config "${expected_config}" --adapter caddyfile)
  [[ "$(proc_uid "${pid}")" == 0 && "$(proc_exe "${pid}")" == "${expected_caddy}" &&
    "${#args[@]}" -eq "${#expected[@]}" ]] || die "live Caddy process identity is ambiguous"
  for index in "${!expected[@]}"; do
    [[ "${args[index]}" == "${expected[index]}" ]] || die "live Caddy arguments are ambiguous"
  done
  [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" == "${ticks}" && "$(proc_uid "${pid}")" == 0 &&
    "$(proc_exe "${pid}")" == "${expected_caddy}" ]] || die "Caddy identity changed before SIGTERM"
  /usr/bin/kill -TERM "${pid}"
  for _ in $(/usr/bin/seq 1 100); do
    [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" || "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] && break
    /usr/bin/sleep .1
  done
  if [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" == "${ticks}" && "$(proc_state "${pid}" 2>/dev/null || true)" != Z ]]; then
    [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" == "${ticks}" &&
      "$(proc_uid "${pid}")" == 0 && "$(proc_exe "${pid}")" == "${expected_caddy}" ]] ||
      die "Caddy identity changed before forced stop"
    /usr/bin/kill -KILL "${pid}"
    for _ in $(/usr/bin/seq 1 100); do
      [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" || "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] && break
      /usr/bin/sleep .1
    done
  fi
  [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" || "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] ||
    die "exact Caddy process did not stop"
  tcp_port_closed 8443 && udp_port_closed 8444 && tcp_port_closed 8543 && udp_port_closed 8544 ||
    die "protected Caddy listeners remain after exact stop"
  /usr/bin/rm -- "${state}"
}

stop_pilot_unit() {
  local active
  [[ "${BOOTSTRAP_ADMISSION}" == 1 ]] || return 0
  active="$(/usr/bin/systemctl is-active "${PILOT_UNIT}" 2>/dev/null || true)"
  case "${active}" in
    active|activating|deactivating)
      /usr/bin/systemctl stop "${PILOT_UNIT}" || return 1
      ;;
    inactive|failed|unknown|"") ;;
    *) return 1 ;;
  esac
  ! /usr/bin/systemctl is-active --quiet "${PILOT_UNIT}" 2>/dev/null
}

stop_pilot_caddy_exact() {
  local lifecycle="$1" expected_caddy expected_config owner_pid network_owned namespace host_veth
  local caddy_owned worker_count pid ticks config live_ticks live_state index
  local -a args=() expected=() keys=() expected_keys=(
    owner_pid network_owned namespace host_veth caddy_owned worker_count
    caddy_pid caddy_start_ticks caddy_config
  )
  expected_caddy="/opt/commu-protocol-pilots/releases/${SERVICE_REPOSITORY_SHA}/repository/traffic_experiment/.tools/caddy"
  expected_config="/opt/commu-protocol-pilots/releases/${SERVICE_REPOSITORY_SHA}/repository/traffic_experiment/configs/Caddyfile.single"
  [[ -f "${lifecycle}" && ! -L "${lifecycle}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${lifecycle}")" == 0:0:600:1 ]] ||
    die "pilot lifecycle state is unsafe"
  mapfile -t keys < <(/usr/bin/awk -F= '{print $1}' "${lifecycle}")
  [[ "${#keys[@]}" -eq "${#expected_keys[@]}" ]] || die "pilot lifecycle key count is malformed"
  for index in "${!expected_keys[@]}"; do
    [[ "${keys[index]}" == "${expected_keys[index]}" ]] || die "pilot lifecycle keys are malformed"
  done
  owner_pid="$(kv_value owner_pid "${lifecycle}")"
  network_owned="$(kv_value network_owned "${lifecycle}")"
  namespace="$(kv_value namespace "${lifecycle}")"
  host_veth="$(kv_value host_veth "${lifecycle}")"
  caddy_owned="$(kv_value caddy_owned "${lifecycle}")"
  worker_count="$(kv_value worker_count "${lifecycle}")"
  pid="$(kv_value caddy_pid "${lifecycle}")"
  ticks="$(kv_value caddy_start_ticks "${lifecycle}")"
  config="$(kv_value caddy_config "${lifecycle}")"
  [[ "${owner_pid}" =~ ^[1-9][0-9]*$ && "${network_owned}" =~ ^[01]$ &&
    "${namespace}" == llm-client && "${host_veth}" == llmhost0 &&
    "${caddy_owned}" =~ ^[01]$ && "${worker_count}" == 1 &&
    "${config}" == "${expected_config}" ]] || die "pilot lifecycle identity is malformed"
  regular_root_file "${expected_caddy}" && regular_root_file "${expected_config}" ||
    die "source-pilot Caddy files are unsafe"
  if [[ "${caddy_owned}" == 0 ]]; then
    [[ -z "${pid}" && -z "${ticks}" ]] || die "unowned pilot Caddy has process identity"
    return 0
  fi
  [[ "${pid}" =~ ^[1-9][0-9]*$ && "${ticks}" =~ ^[1-9][0-9]*$ ]] ||
    die "owned pilot Caddy identity is malformed"
  if [[ ! -r "/proc/${pid}/stat" ]]; then
    return 0
  fi
  live_ticks="$(proc_ticks "${pid}" 2>/dev/null || true)"
  live_state="$(proc_state "${pid}" 2>/dev/null || true)"
  if [[ "${live_ticks}" != "${ticks}" || "${live_state}" == Z ]]; then
    return 0
  fi
  proc_args "${pid}" args || die "cannot read recorded pilot Caddy arguments"
  expected=("${expected_caddy}" run --config "${expected_config}" --adapter caddyfile)
  [[ "$(proc_uid "${pid}")" == 0 && "$(proc_exe "${pid}")" == "${expected_caddy}" &&
    "${#args[@]}" -eq "${#expected[@]}" ]] || die "live pilot Caddy identity is ambiguous"
  for index in "${!expected[@]}"; do
    [[ "${args[index]}" == "${expected[index]}" ]] || die "live pilot Caddy arguments are ambiguous"
  done
  [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" == "${ticks}" &&
    "$(proc_uid "${pid}")" == 0 && "$(proc_exe "${pid}")" == "${expected_caddy}" ]] ||
    die "pilot Caddy identity changed before SIGTERM"
  /usr/bin/kill -TERM "${pid}"
  for _ in $(/usr/bin/seq 1 100); do
    [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" ||
      "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] && break
    /usr/bin/sleep .1
  done
  if [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" == "${ticks}" &&
    "$(proc_state "${pid}" 2>/dev/null || true)" != Z ]]; then
    [[ "$(proc_uid "${pid}")" == 0 && "$(proc_exe "${pid}")" == "${expected_caddy}" ]] ||
      die "pilot Caddy identity changed before forced stop"
    /usr/bin/kill -KILL "${pid}"
    for _ in $(/usr/bin/seq 1 100); do
      [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" ||
        "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] && break
      /usr/bin/sleep .1
    done
  fi
  [[ "$(proc_ticks "${pid}" 2>/dev/null || true)" != "${ticks}" ||
    "$(proc_state "${pid}" 2>/dev/null || true)" == Z ]] ||
    die "exact pilot Caddy process did not stop"
}

cleanup_pilot_bootstrap_resources() {
  local pilot_scope protocol_root network_state network_script global_lock netns_rows link_rows lifecycle
  local -a lifecycle_states=()
  [[ "${BOOTSTRAP_ADMISSION}" == 1 ]] || return 0
  pilot_scope="/var/lib/commu-protocol-pilots/${SERVICE_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}"
  protocol_root="${pilot_scope}/runs/protocol_validation"
  network_state="${pilot_scope}/network_state/llm-client.llmhost0.state"
  network_script="/opt/commu-protocol-pilots/releases/${SERVICE_REPOSITORY_SHA}/repository/traffic_experiment/scripts/11_network_condition.sh"
  global_lock="/run/lock/commu-protocol-pilots/vllm-topology-${SERVICE_UID}.lock"
  [[ -f "${global_lock}" && ! -L "${global_lock}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${global_lock}")" == "0:${SERVICE_GID}:660:1" ]] ||
    die "pilot cleanup topology lock is unsafe"
  exec 6<>"${global_lock}" || die "cannot open pilot cleanup topology lock"
  /usr/bin/flock -w 30 6 || { exec 6>&-; die "pilot cleanup topology remained busy for 30 seconds"; }
  if [[ -e "${protocol_root}" || -L "${protocol_root}" ]]; then
    [[ -d "${protocol_root}" && ! -L "${protocol_root}" &&
      "$(/usr/bin/stat -c %u:%g:%a -- "${protocol_root}")" == 0:0:700 ]] ||
      die "pilot protocol root is unsafe during cleanup"
    mapfile -d '' -t lifecycle_states < <(/usr/bin/find -P "${protocol_root}" -mindepth 1 -maxdepth 1 \
      -type f -name '.lifecycle-[0-9]*.state' -print0)
  fi
  for lifecycle in "${lifecycle_states[@]}"; do
    stop_pilot_caddy_exact "${lifecycle}"
  done
  if [[ -e "${network_state}" || -L "${network_state}" ]]; then
    [[ -f "${network_state}" && ! -L "${network_state}" &&
      "$(/usr/bin/stat -c %u:%g:%a:%h -- "${network_state}")" == 0:0:600:1 ]] ||
      die "pilot network ownership state is unsafe"
    regular_root_file "${network_script}" || die "source-pilot network cleanup script is unsafe"
    /usr/bin/env -i HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
      CLIENT_NETNS=llm-client HOST_VETH=llmhost0 CLIENT_VETH=llmclient0 NETWORK_MTU=1500 \
      NETWORK_STATE_FILE="${network_state}" /usr/bin/bash -p "${network_script}" reset ||
      die "verified pilot network cleanup failed"
  fi
  netns_rows="$(/usr/bin/ip netns list)" || die "cannot verify pilot network namespace cleanup"
  ! /usr/bin/grep -Eq '^llm-client([[:space:]]|$)' <<<"${netns_rows}" ||
    die "pilot network namespace remains after cleanup"
  link_rows="$(/usr/bin/ip -o link show)" || die "cannot verify pilot network link cleanup"
  ! /usr/bin/grep -Eq '^[0-9]+: llmhost0(@[^:[:space:]]+)?:' <<<"${link_rows}" ||
    die "pilot host link remains after cleanup"
  tcp_port_closed 443 && udp_port_closed 443 &&
    tcp_port_closed 8443 && udp_port_closed 8443 &&
    tcp_port_closed 8444 && udp_port_closed 8444 &&
    tcp_port_closed 8543 && udp_port_closed 8543 &&
    tcp_port_closed 8544 && udp_port_closed 8544 ||
    die "protected listener remains after pilot cleanup"
  for lifecycle in "${lifecycle_states[@]}"; do
    /usr/bin/rm -- "${lifecycle}"
  done
  /usr/bin/flock -u 6 || { exec 6>&-; die "could not release pilot cleanup topology lock"; }
  exec 6>&-
}

cleanup_owned_resources() {
  local network_script network_state caddy_state cleanup_failed=0 pilot_unit_stopped=1
  local state_status="" gpu_processes active_units netns_rows link_rows
  if ! stop_pilot_unit; then
    printf 'WARN: pilot bootstrap unit stop could not be proved; preserving its state\n' >&2
    pilot_unit_stopped=0
    cleanup_failed=1
  fi
  if ! stop_service; then
    printf 'WARN: verified service stop failed; continuing exact proxy/network cleanup\n' >&2
    cleanup_failed=1
  fi
  # A completed bootstrap and the measured matrix use the same protected
  # listener names, but never at the same time.  Tear down any matrix-owned
  # proxy/network state first; only then inspect the pilot namespace for
  # leftovers.  Otherwise an interrupted matrix Caddy could be mistaken for a
  # failed pilot cleanup and prevent its own exact teardown.
  caddy_state="/var/lib/commu-secure-matrix/${RUN_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/caddy/matrix-caddy.state"
  stop_caddy_exact "${caddy_state}"
  network_script="/opt/commu-secure-matrix/releases/${MEASUREMENT_REPOSITORY_SHA}/repository/traffic_experiment/scripts/11_network_condition.sh"
  network_state="/var/lib/commu-secure-matrix/${RUN_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/network_state/llm-client.llmhost0.state"
  if [[ -f "${network_state}" && ! -L "${network_state}" ]]; then
    regular_root_file "${network_script}" || die "unsafe network cleanup script"
    NETWORK_STATE_FILE="${network_state}" /usr/bin/bash -p "${network_script}" reset || cleanup_failed=1
  fi
  if (( pilot_unit_stopped == 1 )); then
    # Isolate the strict pilot cleanup so an ambiguous pilot artifact is
    # preserved and reported without aborting the independent matrix/service
    # verification below.  Any flock file descriptor is also closed when the
    # subshell returns.
    if ! (cleanup_pilot_bootstrap_resources); then
      printf 'WARN: verified pilot-bootstrap cleanup failed; preserving its state\n' >&2
      cleanup_failed=1
    fi
  fi
  if ! netns_rows="$(/usr/bin/ip netns list 2>/dev/null)"; then
    cleanup_failed=1
  elif /usr/bin/grep -Eq '^llm-client([[:space:]]|$)' <<<"${netns_rows}"; then
    cleanup_failed=1
  fi
  if ! link_rows="$(/usr/bin/ip -o link show 2>/dev/null)"; then
    cleanup_failed=1
  elif /usr/bin/grep -Eq '^[0-9]+: llmhost0(@[^:[:space:]]+)?:' <<<"${link_rows}"; then
    cleanup_failed=1
  fi
  tcp_port_closed 8000 && tcp_port_closed 8001 && tcp_port_closed 8443 && udp_port_closed 8444 || cleanup_failed=1
  if [[ -e "${STATE}" || -L "${STATE}" ]]; then
    if [[ -f "${STATE}" && ! -L "${STATE}" &&
      "$(/usr/bin/stat -c %u:%g:%a:%h -- "${STATE}")" == "${SERVICE_UID}:${SERVICE_GID}:600:1" ]]; then
      state_status="$(kv_value status "${STATE}" 2>/dev/null || true)"
    fi
    [[ -n "${state_status}" ]] || cleanup_failed=1
    [[ "${state_status}" == stopped ]] || cleanup_failed=1
  else
    # Before a manager publishes state there is no exact unit identity to
    # signal.  Disarm cleanup only when no matching lease unit, selected-GPU
    # process, or listener exists; otherwise retain the independent timer.
    if ! active_units="$(/usr/bin/systemctl list-units --no-legend --plain \
      "commu-vllm-lease-${SERVICE_UID}-*.service" --state=active,activating 2>/dev/null)"; then
      cleanup_failed=1
    elif [[ -n "${active_units}" ]]; then
      cleanup_failed=1
    fi
    if ! gpu_processes="$(/usr/bin/nvidia-smi --query-compute-apps=gpu_uuid,pid \
      --format=csv,noheader,nounits 2>/dev/null)"; then
      cleanup_failed=1
    elif /usr/bin/awk -F, -v wanted="${GPU_UUID}" '
      {gsub(/[[:space:]]/, "", $1); if ($1 == wanted) found=1}
      END {exit found ? 0 : 1}
    ' <<<"${gpu_processes}"; then
      cleanup_failed=1
    fi
  fi
  (( cleanup_failed == 0 ))
}

recover_generation_if_needed() {
  local global_lock
  [[ -d "${MATRIX_ROOT}" && ! -L "${MATRIX_ROOT}" &&
    -f "${MATRIX_ROOT}/RUN_PLAN.json" && ! -L "${MATRIX_ROOT}/RUN_PLAN.json" ]] || return 0
  if /usr/bin/python3 -I "${STATE_TOOL}" verify-generations --root "${MATRIX_ROOT}" >/dev/null 2>&1; then
    return 0
  fi
  global_lock="/run/lock/commu-protocol-pilots/vllm-topology-${SERVICE_UID}.lock"
  [[ -f "${global_lock}" && ! -L "${global_lock}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${global_lock}")" == "0:${SERVICE_GID}:660:1" ]] ||
    return 1
  exec 8<>"${global_lock}" || return 1
  if ! /usr/bin/flock -n 8; then
    exec 8>&-
    return 1
  fi
  # The nonblocking topology lock proves the matrix supervisor and lease
  # manager have exited.  Exact process/listener/network teardown was proved by
  # cleanup_owned_resources before this state-only recovery is attempted.
  if ! /usr/bin/python3 -I "${STATE_TOOL}" recover-open-generation \
    --root "${MATRIX_ROOT}" \
    --orchestration-repository-sha "${REPOSITORY_SHA}" >/dev/null ||
    ! /usr/bin/python3 -I "${STATE_TOOL}" verify-generations \
      --root "${MATRIX_ROOT}" >/dev/null; then
    /usr/bin/flock -u 8 || true
    exec 8>&-
    return 1
  fi
  /usr/bin/flock -u 8 || { exec 8>&-; return 1; }
  exec 8>&-
}

cleanup_owned_resources_locked() {
  local cleanup_rc
  exec 7<>"${CLEANUP_LOCK}" || return 1
  /usr/bin/flock 7 || { exec 7>&-; return 1; }
  if cleanup_owned_resources && recover_generation_if_needed; then cleanup_rc=0; else cleanup_rc=1; fi
  /usr/bin/flock -u 7 || cleanup_rc=1
  exec 7>&-
  return "${cleanup_rc}"
}

segment_main() {
  local id="$1" rc=0
  load_record "${id}"
  exec >>"${MATRIX_LOG}" 2>&1
  cleanup_segment() {
    local stop_ok=1
    rc=$?
    trap - EXIT INT TERM HUP
    if ! cleanup_owned_resources_locked; then
      printf 'WARN: owned-resource cleanup was not verified; leaving expiry timer armed\n' >&2
      stop_ok=0
      rc=1
    fi
    if (( stop_ok == 1 )); then
      /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
    fi
    /usr/bin/date '+MATRIX_SEGMENT_EXIT %F %T %z epoch=%s'
    exit "${rc}"
  }
  trap cleanup_segment EXIT
  trap 'exit 130' INT TERM HUP
  printf '%s  %s\n' "${HELPER_SHA256}" "${HELPER}" "${MANAGER_SHA256}" "${MANAGER}" |
    /usr/bin/sha256sum --check --strict
  [[ "$(sha256_file "${SERVICE_CONFIG}")" == "$(record_value service_config_sha256)" ]] || die "service config changed after launch planning"
  [[ "$(sha256_file "${MEASUREMENT_CONFIG}")" == "$(record_value measurement_config_sha256)" ]] || die "measurement config changed after launch planning"
  [[ "${STATE}" == "${SERVICE_STATE_ROOT}/qwen35-${SERVICE_REPOSITORY_SHA:0:7}/service-matrix-${id}.state" &&
    ! -e "${STATE}" && ! -L "${STATE}" ]] || die "service-state path was occupied before launch"
  [[ "$(/usr/bin/git -c safe.directory="${SERVICE_REPOSITORY_ROOT}" -C "${SERVICE_REPOSITORY_ROOT}" rev-parse HEAD)" == "${SERVICE_REPOSITORY_SHA}" &&
    -z "$(/usr/bin/git -c safe.directory="${SERVICE_REPOSITORY_ROOT}" -C "${SERVICE_REPOSITORY_ROOT}" status --porcelain --untracked-files=no)" ]] ||
    die "service repository identity drifted"
  "${MANAGER}" start --service-user "${SERVICE_USER}" --helper "${HELPER}" \
    --state "${STATE}" --repository-root "${SERVICE_REPOSITORY_ROOT}" \
    --config "${SERVICE_CONFIG}" --log "${VLLM_LOG}" \
    --deadline-epoch "${HARD_DEADLINE_EPOCH}" --timeout 600
  if [[ "${BOOTSTRAP_ADMISSION}" == 1 ]]; then
    /usr/bin/systemd-run --quiet --wait --pipe --collect --service-type=exec \
      --property=KillMode=control-group --unit="${PILOT_UNIT%.service}" \
      /usr/bin/bash -p "${SELF}" _pilot --record-id "${id}"
  fi
  runner_args=(--service-state "${STATE}" --run-id "${RUN_ID}")
  if [[ "${PARENT_MATRIX_ROOT}" != none ]]; then
    runner_args+=(--source-parent-repository-sha "${SERVICE_REPOSITORY_SHA}" \
      --parent-run-repository-sha "${PARENT_RUN_REPOSITORY_SHA}" \
      --parent-matrix-root "${PARENT_MATRIX_ROOT}")
  elif [[ "${SERVICE_REPOSITORY_SHA}" != "${REPOSITORY_SHA}" ]]; then
    runner_args+=(--legacy-run-repository-sha "${SERVICE_REPOSITORY_SHA}")
  fi
  if [[ "${PARENT_MATRIX_ROOT}" == none ]]; then
    "${RUNNER}" check "${runner_args[@]}"
  fi
  if [[ "${MODE}" == new ]]; then
    "${RUNNER}" run "${runner_args[@]}"
  elif [[ "${MODE}" == continue-on-gpu ]]; then
    "${RUNNER}" continue-on-gpu "${runner_args[@]}"
  else
    "${RUNNER}" resume "${runner_args[@]}"
  fi
  printf 'FULL_MATRIX_COMPLETED\n'
}

expire_main() {
  local id="$1"
  load_record "${id}"
  exec >>"${EXPIRY_LOG}" 2>&1
  /usr/bin/date '+EXPIRY_CLEANUP_START %F %T %z epoch=%s'
  if /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "${SESSION}" C-c
    for _ in $(/usr/bin/seq 1 180); do
      /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null || break
      /usr/bin/sleep 1
    done
  fi
  if /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
    /usr/bin/tmux kill-session -t "${SESSION}"
    /usr/bin/sleep 5
  fi
  cleanup_owned_resources_locked || die "owned-resource cleanup could not be verified"
  /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null && die "matrix tmux remains after expiry"
  /usr/bin/date '+EXPIRY_CLEANUP_OK %F %T %z epoch=%s'
}

ACTION="${1:-}"
[[ -n "${ACTION}" ]] && shift || true
release_precheck
case "${ACTION}" in
  _segment|_expire|_pilot)
    [[ $# -eq 2 && "$1" == --record-id && "$2" =~ ^[0-9a-f]{16}$ ]] || die "invalid internal invocation"
    if [[ "${ACTION}" == _segment ]]; then
      segment_main "$2"
    elif [[ "${ACTION}" == _pilot ]]; then
      pilot_main "$2"
    else
      expire_main "$2"
    fi
    exit
    ;;
  new|resume|continue-on-gpu) MODE="${ACTION}" ;;
  *) usage ;;
esac

RUN_ID=""; LEASE=""; GPU_INDEX_ASSERTION=""; SOURCE_REPOSITORY_SHA=""; SERVICE_CONFIG_REQUESTED=""
AUTHORIZATION_CUTOFF_EPOCH=""
PARENT_RUN_ID=""; PARENT_RUN_REPOSITORY_SHA=""; PARENT_MATRIX_ROOT=""
BOOTSTRAP_ADMISSION=0; BOOTSTRAP_ADMISSION_SET=0; SOURCE_PILOT_RUNNER=none
while (($#)); do
  case "$1" in
    --run-id) [[ $# -ge 2 && -z "${RUN_ID}" ]] || usage; RUN_ID="$2"; shift 2 ;;
    --lease) [[ $# -ge 2 && -z "${LEASE}" ]] || usage; LEASE="$2"; shift 2 ;;
    --gpu-index) [[ $# -ge 2 && -z "${GPU_INDEX_ASSERTION}" ]] || usage; GPU_INDEX_ASSERTION="$2"; shift 2 ;;
    --source-repository-sha) [[ $# -ge 2 && -z "${SOURCE_REPOSITORY_SHA}" ]] || usage; SOURCE_REPOSITORY_SHA="$2"; shift 2 ;;
    --authorization-cutoff-epoch) [[ $# -ge 2 && -z "${AUTHORIZATION_CUTOFF_EPOCH}" ]] || usage; AUTHORIZATION_CUTOFF_EPOCH="$2"; shift 2 ;;
    --parent-run-id) [[ $# -ge 2 && -z "${PARENT_RUN_ID}" ]] || usage; PARENT_RUN_ID="$2"; shift 2 ;;
    --parent-run-repository-sha|--parent-source-repository-sha)
      [[ $# -ge 2 && -z "${PARENT_RUN_REPOSITORY_SHA}" ]] || usage
      PARENT_RUN_REPOSITORY_SHA="$2"; shift 2 ;;
    --bootstrap-admission-if-missing)
      [[ "${BOOTSTRAP_ADMISSION_SET}" -eq 0 ]] || usage
      BOOTSTRAP_ADMISSION=1; BOOTSTRAP_ADMISSION_SET=1; shift ;;
    --service-config-template) [[ $# -ge 2 && -z "${SERVICE_CONFIG_REQUESTED}" ]] || usage; SERVICE_CONFIG_REQUESTED="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ "${RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ && "${LEASE}" =~ ^(0|[1-9][0-9]*)(m|h)?$ &&
  ( -z "${GPU_INDEX_ASSERTION}" || "${GPU_INDEX_ASSERTION}" =~ ^(0|[1-9][0-9]*)$ ) &&
  ( -z "${SOURCE_REPOSITORY_SHA}" || "${SOURCE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ) &&
  ( -z "${AUTHORIZATION_CUTOFF_EPOCH}" || "${AUTHORIZATION_CUTOFF_EPOCH}" =~ ^(0|[1-9][0-9]*)$ ) &&
  ( -z "${PARENT_RUN_ID}" || "${PARENT_RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ) &&
  ( -z "${PARENT_RUN_REPOSITORY_SHA}" || "${PARENT_RUN_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ) ]] || usage
if [[ "${MODE}" != continue-on-gpu &&
  ( -n "${PARENT_RUN_ID}" || -n "${PARENT_RUN_REPOSITORY_SHA}" || "${BOOTSTRAP_ADMISSION}" -eq 1 ) ]]; then
  usage
fi
if [[ -n "${AUTHORIZATION_CUTOFF_EPOCH}" && "${MODE}" != resume && "${MODE}" != continue-on-gpu ]]; then
  die "--authorization-cutoff-epoch is only valid for resume or continue-on-gpu"
fi

PLAN=""; PLAN_SHA256=""; PLAN_ACTIVE_CONFIG_SHA256=""; RUN_REPOSITORY_SHA=""
if [[ "${MODE}" == new ]]; then
  [[ -n "${GPU_INDEX_ASSERTION}" ]] || die "a new run requires --gpu-index"
  [[ -z "${SOURCE_REPOSITORY_SHA}" ]] || die "--source-repository-sha is only a resume assertion"
  GPU_INDEX="${GPU_INDEX_ASSERTION}"
  resolve_gpu
  RUN_REPOSITORY_SHA="${REPOSITORY_SHA}"
elif [[ "${MODE}" == continue-on-gpu ]]; then
  [[ -n "${GPU_INDEX_ASSERTION}" && -n "${PARENT_RUN_ID}" &&
    -n "${PARENT_RUN_REPOSITORY_SHA}" ]] || die "continue-on-gpu requires parent run/source and target GPU"
  [[ -z "${SOURCE_REPOSITORY_SHA}" ]] || die "use --parent-run-repository-sha for continue-on-gpu"
  [[ "${RUN_ID}" != "${PARENT_RUN_ID}" ]] || die "target run ID must differ from parent run ID"
  find_parent_plan
  GPU_INDEX="${GPU_INDEX_ASSERTION}"
  resolve_gpu
  [[ "${GPU_UUID}" != "${PARENT_GPU_UUID}" ]] || die "target GPU must differ from the immutable parent GPU"
  RUN_REPOSITORY_SHA="${REPOSITORY_SHA}"
else
  [[ -z "${PARENT_RUN_ID}" && -z "${PARENT_RUN_REPOSITORY_SHA}" ]] || usage
  find_resume_plan
  live_plan_uuid="${GPU_UUID}"
  resolve_gpu
  [[ "${GPU_UUID}" == "${live_plan_uuid}" ]] || die "physical GPU UUID differs from immutable RUN_PLAN.json"
fi
if [[ "${MODE}" == continue-on-gpu ]]; then
  [[ "${SERVICE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "parent plan has no frozen service source identity"
elif [[ -n "${PARENT_MATRIX_ROOT}" ]]; then
  [[ "${SERVICE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ && "${PARENT_RUN_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ ]] ||
    die "continuation plan has no parent/source identity"
else
  SERVICE_REPOSITORY_SHA="${RUN_REPOSITORY_SHA}"
fi
if [[ "${MODE}" == new ]]; then
  [[ "${SERVICE_REPOSITORY_SHA}" == "${REPOSITORY_SHA}" && "${PILOT_SHA}" == "${REPOSITORY_SHA}" ]] ||
    die "a new run requires a matrix release whose pilot policy is the same current commit"
else
  verify_measurement_source "${SERVICE_REPOSITORY_SHA}"
fi
if [[ "${MODE}" == new ]]; then
  verify_measurement_source "${SERVICE_REPOSITORY_SHA}"
fi
MEASUREMENT_REPOSITORY_SHA="${SERVICE_REPOSITORY_SHA}"
if [[ -n "${PARENT_MATRIX_ROOT}" ]]; then
  # Continue with this release's ledger-aware request runner.  The supervisor
  # independently proves its measurement payload is byte-identical to the
  # immutable parent release before touching the target root.
  MEASUREMENT_REPOSITORY_SHA="${REPOSITORY_SHA}"
  MEASUREMENT_RELEASE_ROOT="${RELEASE_ROOT}"
  MEASUREMENT_CONFIG="${RELEASE_ROOT}/config/server.env"
  MEASUREMENT_CONFIG_SHA256="$(sha256_file "${MEASUREMENT_CONFIG}")"
fi
ADMISSION="/var/lib/commu-protocol-pilots/${SERVICE_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/runs/protocol_validation/PROTOCOL_VALIDATION_OK"
if [[ -e "${ADMISSION}" || -L "${ADMISSION}" ]]; then
  [[ -f "${ADMISSION}" && ! -L "${ADMISSION}" &&
    "$(/usr/bin/stat -c %u:%a:%h -- "${ADMISSION}")" == 0:444:1 ]] ||
    die "selected source/GPU protocol admission path is unsafe"
  BOOTSTRAP_ADMISSION=0
  SOURCE_PILOT_RUNNER=none
elif [[ "${BOOTSTRAP_ADMISSION}" == 1 ]]; then
  [[ "${MODE}" == continue-on-gpu &&
    "${SOURCE_PILOT_RUNNER}" == "/opt/commu-protocol-pilots/releases/${SERVICE_REPOSITORY_SHA}/repository/traffic_experiment/scripts/28_run_privileged_protocol_pilots.sh" ]] ||
    die "pilot bootstrap identity is not the frozen service source"
  regular_root_file "${SOURCE_PILOT_RUNNER}" || die "source pilot runner is unsafe"
else
  die "selected source/GPU has no safe protocol admission marker; run the pilot first"
fi

if [[ -z "${SERVICE_CONFIG_REQUESTED}" ]]; then
  SERVICE_CONFIG_REQUESTED="${SERVICE_STATE_ROOT}/qwen35-${SERVICE_REPOSITORY_SHA:0:7}/server.gpu${GPU_INDEX}.single.env"
  if [[ ! -e "${SERVICE_CONFIG_REQUESTED}" && ! -L "${SERVICE_CONFIG_REQUESTED}" ]]; then
    mapfile -t template_candidates < <(/usr/bin/find -P "${SERVICE_STATE_ROOT}/qwen35-${SERVICE_REPOSITORY_SHA:0:7}" \
      -mindepth 1 -maxdepth 1 -type f -name 'server.gpu*.single.env' -print | /usr/bin/sort)
    [[ "${#template_candidates[@]}" -ge 1 ]] ||
      die "no reviewed single-GPU service config exists for source ${SERVICE_REPOSITORY_SHA:0:7}"
    SERVICE_CONFIG_REQUESTED="${template_candidates[0]}"
  fi
fi
validate_service_template "${SERVICE_CONFIG_REQUESTED}"

SERVICE_HOME="$(/usr/bin/getent passwd "${SERVICE_USER}" | /usr/bin/awk -F: 'NR == 1 {print $6}')" || die "cannot resolve service home"
[[ "${SERVICE_HOME}" == "/home/${SERVICE_USER}" ]] || die "service home is outside reviewed policy"
SERVICE_REPOSITORY_ROOT="$(/usr/bin/dirname -- "$(/usr/bin/dirname -- "$(/usr/bin/dirname -- "${EXPECTED_CONTROLLER_LAUNCHER}")")")"
SERVICE_REPOSITORY_ROOT="$(/usr/bin/readlink -e -- "${SERVICE_REPOSITORY_ROOT}")" || die "service repository is missing"
[[ "${SERVICE_REPOSITORY_ROOT}" == "${SERVICE_HOME}/commu" &&
  "$(/usr/bin/readlink -e -- "${EXPECTED_CONTROLLER_LAUNCHER}")" == "${EXPECTED_CONTROLLER_LAUNCHER}" &&
  "$(/usr/bin/git -c safe.directory="${SERVICE_REPOSITORY_ROOT}" -C "${SERVICE_REPOSITORY_ROOT}" rev-parse HEAD)" == "${SERVICE_REPOSITORY_SHA}" &&
  -z "$(/usr/bin/git -c safe.directory="${SERVICE_REPOSITORY_ROOT}" -C "${SERVICE_REPOSITORY_ROOT}" status --porcelain --untracked-files=no)" ]] ||
  die "service repository is not the clean pilot-policy commit"

gpu_processes="$(/usr/bin/nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)" || die "cannot query GPU processes"
/usr/bin/awk -F, -v wanted="${GPU_UUID}" '{gsub(/[[:space:]]/, "", $1); if ($1 == wanted) found=1} END {exit found ? 0 : 1}' <<<"${gpu_processes}" &&
  die "selected GPU ${GPU_INDEX} is occupied"
tcp_listeners="$(/usr/bin/ss -H -lnt)" || die "cannot query required TCP listeners"
udp_listeners="$(/usr/bin/ss -H -lnu)" || die "cannot query required UDP listeners"
for protected_port in 443 8443 8444 8543 8544; do
  ! /usr/bin/grep -Eq ":${protected_port}[[:space:]]" <<<"${tcp_listeners}" ||
    die "protected TCP port ${protected_port} is occupied"
  ! /usr/bin/grep -Eq ":${protected_port}[[:space:]]" <<<"${udp_listeners}" ||
    die "protected UDP port ${protected_port} is occupied"
done
for service_port in 8000 8001; do
  ! /usr/bin/grep -Eq ":${service_port}[[:space:]]" <<<"${tcp_listeners}" ||
    die "required service TCP port ${service_port} is occupied"
done
netns_rows="$(/usr/bin/ip netns list)" || die "cannot query network namespaces"
/usr/bin/grep -Eq '^llm-client([[:space:]]|$)' <<<"${netns_rows}" && die "project network namespace already exists"
link_rows="$(/usr/bin/ip -o link show)" || die "cannot query network links"
/usr/bin/awk -F': ' '{name=$2; sub(/@.*/, "", name); print name}' <<<"${link_rows}" |
  /usr/bin/grep -Fxq llmhost0 && die "project host link already exists"

/usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${RECORDS_ROOT}"
for _ in $(/usr/bin/seq 1 100); do
  RECORD_ID="$(/usr/bin/openssl rand -hex 8)"
  [[ "${RECORD_ID}" =~ ^[0-9a-f]{16}$ ]] || continue
  RECORD_DIR="${RECORDS_ROOT}/${RECORD_ID}"
  /usr/bin/mkdir --mode=0700 -- "${RECORD_DIR}" 2>/dev/null && break
  RECORD_DIR=""
done
[[ -n "${RECORD_DIR:-}" ]] || die "cannot allocate a unique segment record"
/usr/bin/chown root:"${SERVICE_GID}" "${RECORD_DIR}"
/usr/bin/chmod 0710 "${RECORD_DIR}"
/usr/bin/install -o root -g root -m 0600 /dev/null "${RECORD_DIR}/cleanup.lock"
SESSION="commu-matrix-${RECORD_ID}"
TIMER_BASE="${SESSION}-expiry"
if [[ "${BOOTSTRAP_ADMISSION}" == 1 ]]; then
  PILOT_UNIT="${SESSION}-pilot.service"
else
  PILOT_UNIT=none
fi
ATTEMPT_DIR="${SERVICE_STATE_ROOT}/qwen35-${SERVICE_REPOSITORY_SHA:0:7}/matrix-segments/${RECORD_ID}"
/usr/bin/install -d -o "${SERVICE_UID}" -g "${SERVICE_GID}" -m 0700 "${ATTEMPT_DIR}"
SERVICE_RUNTIME_ROOT="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" ensure-stable-runtime-root \
  --runtime-namespace-root "${SERVICE_RUNTIME_NAMESPACE}" \
  --source-repository-sha "${SERVICE_REPOSITORY_SHA}" \
  --run-id "${RUN_ID}" --gpu-index "${GPU_INDEX}" --gpu-uuid "${GPU_UUID}" \
  --service-uid "${SERVICE_UID}" --service-gid "${SERVICE_GID}")" ||
  die "could not derive the stable per-run service runtime root"
STATE="${SERVICE_STATE_ROOT}/qwen35-${SERVICE_REPOSITORY_SHA:0:7}/service-matrix-${RECORD_ID}.state"
[[ ! -e "${STATE}" && ! -L "${STATE}" ]] || die "allocated service-state path already exists"
VLLM_LOG="${ATTEMPT_DIR}/vllm.log"
MATRIX_LOG="${RECORD_DIR}/matrix.log"
EXPIRY_LOG="${RECORD_DIR}/expiry.log"
NOW_EPOCH="$(/usr/bin/date +%s)"
deadline_args=(deadline --now "${NOW_EPOCH}" --lease "${LEASE}")
if [[ -n "${AUTHORIZATION_CUTOFF_EPOCH}" ]]; then
  deadline_args+=(--authorization-cutoff "${AUTHORIZATION_CUTOFF_EPOCH}")
fi
deadline_fields="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" "${deadline_args[@]}")" ||
  die "lease duration or authorization cutoff was rejected"
IFS=$'\t' read -r deadline_now LEASE_MINUTES CLEANUP_EPOCH HARD_DEADLINE_EPOCH <<<"${deadline_fields}"
[[ "${deadline_now}" == "${NOW_EPOCH}" && "${LEASE_MINUTES}" =~ ^[0-9]+$ &&
  "${CLEANUP_EPOCH}" =~ ^[0-9]+$ && "${HARD_DEADLINE_EPOCH}" =~ ^[0-9]+$ ]] || die "deadline helper returned malformed output"
INITIAL_DELAY_SECONDS=$((CLEANUP_EPOCH - NOW_EPOCH))
[[ "${INITIAL_DELAY_SECONDS}" -ge 600 ]] || die "lease is too short to arm reviewed cleanup"

SERVICE_CONFIG="${ATTEMPT_DIR}/service.env"
materialize_args=(
  --template "${SERVICE_TEMPLATE}" --output "${SERVICE_CONFIG}"
  --gpu-index "${GPU_INDEX}" --gpu-uuid "${GPU_UUID}"
  --source-repository-sha "${SERVICE_REPOSITORY_SHA}"
  --expected-vllm-bin "${EXPECTED_API_VLLM}"
  --expected-ld-library-path "${EXPECTED_API_LD_LIBRARY_PATH}"
  --measurement-config "${MEASUREMENT_CONFIG}"
  --measurement-repository-sha "${MEASUREMENT_REPOSITORY_SHA}"
  --service-runtime-root "${SERVICE_RUNTIME_ROOT}"
)
if [[ "${MODE}" == resume ]]; then
  materialize_args+=(--expected-active-config-sha256 "${PLAN_ACTIVE_CONFIG_SHA256}")
fi
/usr/bin/python3 -I "${LAUNCH_HELPER}" materialize-service-config \
  "${materialize_args[@]}" || die "could not materialize the service config"
/usr/bin/chown "${SERVICE_UID}:${SERVICE_GID}" "${SERVICE_CONFIG}"
/usr/bin/chmod 0600 "${SERVICE_CONFIG}"
SERVICE_CONFIG_SHA256="$(sha256_file "${SERVICE_CONFIG}")"
if [[ "${MODE}" == resume ]]; then
  [[ "${SERVICE_CONFIG_SHA256}" == "${PLAN_ACTIVE_CONFIG_SHA256}" ]] ||
    die "service config digest differs from immutable run plan"
fi

RECORD="${RECORD_DIR}/record.state"
{
  printf 'schema=commu-matrix-segment-record-v1\nrecord_id=%s\nsession=%s\ntimer_base=%s\n' "${RECORD_ID}" "${SESSION}" "${TIMER_BASE}"
  printf 'release_repository_sha=%s\nrun_repository_sha=%s\nservice_repository_sha=%s\nrun_id=%s\nmode=%s\n' "${REPOSITORY_SHA}" "${RUN_REPOSITORY_SHA}" "${SERVICE_REPOSITORY_SHA}" "${RUN_ID}" "${MODE}"
  printf 'parent_matrix_root=%s\n' "${PARENT_MATRIX_ROOT:-none}"
  printf 'parent_run_repository_sha=%s\n' "${PARENT_RUN_REPOSITORY_SHA:-none}"
  printf 'bootstrap_admission=%s\nsource_pilot_runner=%s\npilot_unit=%s\n' \
    "${BOOTSTRAP_ADMISSION}" "${SOURCE_PILOT_RUNNER}" "${PILOT_UNIT}"
  printf 'measurement_repository_sha=%s\n' "${MEASUREMENT_REPOSITORY_SHA}"
  printf 'gpu_index=%s\ngpu_uuid=%s\nplan_sha256=%s\n' "${GPU_INDEX}" "${GPU_UUID}" "${PLAN_SHA256:-none}"
  printf 'service_repository_root=%s\nservice_config=%s\nservice_config_sha256=%s\nservice_state=%s\n' "${SERVICE_REPOSITORY_ROOT}" "${SERVICE_CONFIG}" "${SERVICE_CONFIG_SHA256}" "${STATE}"
  printf 'service_runtime_root=%s\n' "${SERVICE_RUNTIME_ROOT}"
  printf 'measurement_config=%s\nmeasurement_config_sha256=%s\n' "${MEASUREMENT_CONFIG}" "${MEASUREMENT_CONFIG_SHA256}"
  printf 'vllm_log=%s\nmatrix_log=%s\ncreated_epoch=%s\ncleanup_epoch=%s\nhard_deadline_epoch=%s\n' "${VLLM_LOG}" "${MATRIX_LOG}" "${NOW_EPOCH}" "${CLEANUP_EPOCH}" "${HARD_DEADLINE_EPOCH}"
} >"${RECORD}"
/usr/bin/chown root:root "${RECORD}"
/usr/bin/chmod 0600 "${RECORD}"
/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${MATRIX_LOG}"
/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${EXPIRY_LOG}"

TIMER_ARM_EPOCH="$(/usr/bin/date +%s)"
DELAY_SECONDS=$((CLEANUP_EPOCH - TIMER_ARM_EPOCH))
[[ "${DELAY_SECONDS}" -ge 60 ]] || die "not enough lease time remains to start the matrix segment"
/usr/bin/systemd-run --unit="${TIMER_BASE}" --on-active="${DELAY_SECONDS}s" \
  --timer-property=AccuracySec=1s /usr/bin/bash -p "${SELF}" _expire --record-id "${RECORD_ID}"
if ! /usr/bin/tmux new-session -d -s "${SESSION}" "${SELF} _segment --record-id ${RECORD_ID}"; then
  load_record "${RECORD_ID}"
  if cleanup_owned_resources_locked; then
    /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
  else
    printf 'WARN: tmux launch failed and cleanup was not verified; expiry timer remains armed\n' >&2
  fi
  die "could not create the matrix tmux session"
fi
/usr/bin/sleep 5
if ! /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
  load_record "${RECORD_ID}"
  if cleanup_owned_resources_locked; then
    /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
  else
    printf 'WARN: early segment exit cleanup failed; expiry timer remains armed\n' >&2
  fi
  die "matrix segment exited immediately; inspect ${MATRIX_LOG}"
fi

printf 'MATRIX_SEGMENT_SUBMITTED session=%s record=%s\n' "${SESSION}" "${RECORD}"
printf 'mode=%s run_id=%s gpu_index=%s gpu_uuid=%s\n' "${MODE}" "${RUN_ID}" "${GPU_INDEX}" "${GPU_UUID}"
printf 'cleanup_local=%s cleanup_epoch=%s\n' "$(/usr/bin/date -d "@${CLEANUP_EPOCH}" '+%F %T %z')" "${CLEANUP_EPOCH}"
printf 'hard_deadline_local=%s hard_deadline_epoch=%s\n' "$(/usr/bin/date -d "@${HARD_DEADLINE_EPOCH}" '+%F %T %z')" "${HARD_DEADLINE_EPOCH}"
printf 'matrix_log=%s\nvllm_log=%s\n' "${MATRIX_LOG}" "${VLLM_LOG}"
