#!/usr/bin/bash -p
set -euo pipefail
set +x
umask 077

# Queue one bounded resume without reserving a GPU while it is occupied.
# The immutable RUN_PLAN.json selects the exact GPU; this script never changes it.

FIXED_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
WAITERS_ROOT=/var/lib/commu-matrix-waiters
WAIT_LOCK_ROOT=/run/lock/commu-matrix-waiters
POLL_SECONDS=15

CLEAN_ENV_DECLARATION="$(declare -p COMMU_MATRIX_WAITER_CLEAN_ENV 2>/dev/null || true)"
if [[ "${CLEAN_ENV_DECLARATION}" != 'declare -r COMMU_MATRIX_WAITER_CLEAN_ENV="1"' ]]; then
  [[ "${EUID}" -eq 0 ]] || { printf 'ERROR: waiter must run as root\n' >&2; exit 2; }
  SELF="$(/usr/bin/readlink -e -- "${BASH_SOURCE[0]}")" || exit 2
  [[ -f "${SELF}" && ! -L "${SELF}" && "$(/usr/bin/stat -c %u -- "${SELF}")" == 0 ]] || exit 2
  exec /usr/bin/env -i \
    HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC PATH="${FIXED_PATH}" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
    /usr/bin/bash -p -c '
      readonly COMMU_MATRIX_WAITER_CLEAN_ENV=1
      waiter="$1"
      shift
      source "${waiter}" "$@"
    ' commu-matrix-waiter-clean-env "${SELF}" "$@"
fi
PATH="${FIXED_PATH}"
export PATH HOME LANG LC_ALL TZ PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE PYTHONSAFEPATH
[[ "$(declare -p COMMU_MATRIX_WAITER_CLEAN_ENV 2>/dev/null || true)" == \
  'declare -r COMMU_MATRIX_WAITER_CLEAN_ENV="1"' ]] || {
  printf 'ERROR: clean-environment shell marker drifted\n' >&2
  exit 2
}
while IFS='=' read -r inherited_name _; do
  case "${inherited_name}" in
    HOME|LANG|LC_ALL|TZ|PATH|PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|PYTHONSAFEPATH|PWD|SHLVL|_) ;;
    *) printf 'ERROR: unsanitized environment variable: %s\n' "${inherited_name}" >&2; exit 2 ;;
  esac
done < <(/usr/bin/env)
[[ "${EUID}" -eq 0 ]] || { printf 'ERROR: waiter must run as root\n' >&2; exit 2; }
cd /
unset OLDPWD

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
usage() {
  cat >&2 <<'EOF'
Usage:
  33_wait_for_privileged_matrix_gpu.sh wait-resume \
    --run-id SAFE_ID --source-repository-sha 40_HEX --gpu-index N \
    --authorization-cutoff-epoch EPOCH [--max-lease 110m] \
    [--service-config-template POLICY_SCOPED_PATH]

The immutable run plan selects the GPU. --gpu-index is an assertion, not an
override. The waiter holds no GPU or topology lock while polling, starts one
ordinary resume at most once, and expires at the absolute authorization cutoff.
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

SELF="$(/usr/bin/readlink -e -- "${BASH_SOURCE[0]}")" || die "cannot resolve waiter"
SCRIPT_DIR="$(/usr/bin/dirname -- "${SELF}")"
EXPERIMENT_ROOT="$(/usr/bin/readlink -e -- "${SCRIPT_DIR}/..")" || die "cannot resolve experiment root"
REPOSITORY_ROOT="$(/usr/bin/readlink -e -- "${EXPERIMENT_ROOT}/..")" || die "cannot resolve release repository"
RELEASE_ROOT="$(/usr/bin/readlink -e -- "${REPOSITORY_ROOT}/..")" || die "cannot resolve release root"
METADATA="${RELEASE_ROOT}/RELEASE_METADATA"
MANIFEST="${RELEASE_ROOT}/RELEASE_FILES.sha256"
RUNTIME_MANIFEST="${RELEASE_ROOT}/INSTALLED_RUNTIME_FILES.sha256"
POLICY="${RELEASE_ROOT}/policy/service.state"
LAUNCHER="${SCRIPT_DIR}/32_launch_privileged_matrix_segment.sh"
LAUNCH_HELPER="${SCRIPT_DIR}/privileged_matrix_launch.py"
STATE_TOOL="${SCRIPT_DIR}/privileged_matrix_state.py"

release_precheck() {
  local executable path mode
  case "${RELEASE_ROOT}" in /opt/commu-secure-matrix/releases/[0-9a-f][0-9a-f]*) ;; *) die "waiter is outside the fixed release root" ;; esac
  for path in /opt /opt/commu-secure-matrix /opt/commu-secure-matrix/releases \
    "${RELEASE_ROOT}" "${REPOSITORY_ROOT}" "${EXPERIMENT_ROOT}" "${SCRIPT_DIR}"; do
    trusted_root_dir "${path}" || die "unsafe release directory: ${path}"
  done
  for path in "${SELF}" "${METADATA}" "${MANIFEST}" "${RUNTIME_MANIFEST}" "${POLICY}" \
    "${LAUNCHER}" "${LAUNCH_HELPER}" "${STATE_TOOL}"; do
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
  SERVICE_USER="$(kv_value service_user "${POLICY}")" || die "missing service user"
  SERVICE_UID="$(kv_value service_uid "${POLICY}")" || die "missing service UID"
  SERVICE_GID="$(kv_value service_gid "${POLICY}")" || die "missing service GID"
  [[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*$ && "${SERVICE_UID}" =~ ^[1-9][0-9]*$ &&
    "${SERVICE_GID}" =~ ^[1-9][0-9]*$ ]] || die "unsafe service identity"
  [[ "$(/usr/bin/id -u "${SERVICE_USER}")" == "${SERVICE_UID}" &&
    "$(/usr/bin/id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] || die "service account identity drifted"
  for executable in /usr/bin/awk /usr/bin/basename /usr/bin/chmod /usr/bin/chown \
    /usr/bin/date /usr/bin/dirname /usr/bin/flock /usr/bin/grep /usr/bin/id /usr/bin/install \
    /usr/bin/ip /usr/bin/mkdir /usr/bin/mv /usr/bin/nvidia-smi /usr/bin/openssl /usr/bin/python3 \
    /usr/bin/readlink /usr/bin/rm /usr/bin/seq /usr/bin/sha256sum /usr/bin/sleep /usr/bin/ss \
    /usr/bin/stat /usr/bin/systemctl /usr/bin/systemd-run /usr/bin/timeout /usr/bin/tmux; do
    [[ -x "${executable}" && "$(/usr/bin/stat -c %u -- "${executable}")" == 0 ]] ||
      die "unsafe or missing required executable: ${executable}"
    mode="$(/usr/bin/stat -c %a -- "${executable}")" || die "cannot inspect ${executable}"
    (( (8#${mode} & 8#022) == 0 )) || die "required executable is group/world writable: ${executable}"
  done
}

record_value() { kv_value "$1" "${RECORD}"; }
set_wait_outcome() {
  local state="$1" temporary
  [[ "${state}" =~ ^(queued|waiting|launching|submitted|not_needed|expired_without_launch|failed)$ ]] ||
    return 1
  temporary="${OUTCOME}.tmp.$$"
  [[ ! -e "${temporary}" && ! -L "${temporary}" ]] || return 1
  /usr/bin/install -o root -g "${RECORD_GID}" -m 0640 /dev/null "${temporary}" || return 1
  printf 'state=%s\nupdated_epoch=%s\n' "${state}" "$(/usr/bin/date +%s)" >"${temporary}" || return 1
  /usr/bin/mv -- "${temporary}" "${OUTCOME}" || return 1
}
load_expiry_record() {
  local id="$1"
  [[ "${id}" =~ ^[0-9a-f]{16}$ ]] || die "invalid internal waiter record ID"
  waiters_identity="$(/usr/bin/stat -c %u:%g:%a -- "${WAITERS_ROOT}" 2>/dev/null)" || die "missing waiter records root"
  IFS=: read -r waiters_uid RECORD_GID waiters_mode <<<"${waiters_identity}"
  [[ -d "${WAITERS_ROOT}" && ! -L "${WAITERS_ROOT}" && "${waiters_uid}" == 0 &&
    "${RECORD_GID}" =~ ^[1-9][0-9]*$ && "${waiters_mode}" == 710 ]] || die "unsafe waiter records root"
  [[ -z "${SERVICE_GID:-}" || "${RECORD_GID}" == "${SERVICE_GID}" ]] || die "waiter record group drifted"
  RECORD_DIR="${WAITERS_ROOT}/${id}"
  RECORD="${RECORD_DIR}/record.state"
  [[ -d "${RECORD_DIR}" && ! -L "${RECORD_DIR}" &&
    "$(/usr/bin/stat -c %u:%g:%a -- "${RECORD_DIR}")" == "0:${RECORD_GID}:710" ]] ||
    die "unsafe waiter record directory"
  [[ -f "${RECORD}" && ! -L "${RECORD}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${RECORD}")" == 0:0:600:1 ]] || die "unsafe waiter record"
  RECORD_SCHEMA="$(record_value schema)"
  RECORD_ID="$(record_value record_id)"; TARGET_ID="$(record_value target_id)"
  SESSION="$(record_value session)"; TIMER_BASE="$(record_value timer_base)"
  AUTHORIZATION_CUTOFF_EPOCH="$(record_value authorization_cutoff_epoch)"
  WAIT_LOG="${RECORD_DIR}/wait.log"; OUTCOME="${RECORD_DIR}/outcome.state"
  [[ "${RECORD_SCHEMA}" == commu-matrix-waiter-record-v1 && "${RECORD_ID}" == "${id}" &&
    "${TARGET_ID}" =~ ^[0-9a-f]{16}$ && "${SESSION}" == "commu-matrix-wait-${RECORD_ID}" &&
    "${TIMER_BASE}" == "${SESSION}-expiry" && "${AUTHORIZATION_CUTOFF_EPOCH}" =~ ^(0|[1-9][0-9]*)$ ]] ||
    die "minimal waiter expiry identity is malformed"
  [[ -f "${WAIT_LOG}" && ! -L "${WAIT_LOG}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${WAIT_LOG}")" == "0:${RECORD_GID}:640:1" ]] || die "unsafe waiter log"
  [[ -f "${OUTCOME}" && ! -L "${OUTCOME}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${OUTCOME}")" == "0:${RECORD_GID}:640:1" ]] || die "unsafe waiter outcome"
}
load_wait_record() {
  local id="$1"
  load_expiry_record "${id}"
  RELEASE_REPOSITORY_SHA="$(record_value release_repository_sha)"
  RUN_ID="$(record_value run_id)"; SOURCE_REPOSITORY_SHA="$(record_value source_repository_sha)"
  GPU_INDEX="$(record_value gpu_index)"; GPU_UUID="$(record_value gpu_uuid)"
  PLAN="$(record_value plan)"; PLAN_SHA256="$(record_value plan_sha256)"
  MAX_LEASE="$(record_value max_lease)"; SERVICE_CONFIG_TEMPLATE="$(record_value service_config_template)"
  TARGET_LOCK="$(record_value target_lock)"; ACTIVE_REGISTRATION="$(record_value active_registration)"
  MATRIX_ROOT="$(/usr/bin/dirname -- "${PLAN}")"
  [[ "${RECORD_SCHEMA}" == commu-matrix-waiter-record-v1 &&
    "${RELEASE_REPOSITORY_SHA}" == "${REPOSITORY_SHA}" &&
    "${RECORD_ID}" == "${id}" &&
    "${RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ && "${SOURCE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ &&
    "${GPU_INDEX}" =~ ^(0|[1-9][0-9]*)$ && "${GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ &&
    "${PLAN}" == "/var/lib/commu-secure-matrix/${SOURCE_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/runs/${RUN_ID}/RUN_PLAN.json" &&
    "${PLAN_SHA256}" =~ ^[0-9a-f]{64}$ && "${AUTHORIZATION_CUTOFF_EPOCH}" =~ ^(0|[1-9][0-9]*)$ &&
    "${MAX_LEASE}" =~ ^(0|[1-9][0-9]*)(m|h)?$ &&
    "${TARGET_LOCK}" == "${WAIT_LOCK_ROOT}/target-${TARGET_ID}.lock" &&
    "${ACTIVE_REGISTRATION}" == "${WAIT_LOCK_ROOT}/active-${TARGET_ID}.state" ]] || die "waiter record identity is malformed"
  [[ "${SERVICE_CONFIG_TEMPLATE}" == none ||
    ( "${SERVICE_CONFIG_TEMPLATE}" = /* && "${SERVICE_CONFIG_TEMPLATE}" != *$'\n'* ) ]] ||
    die "waiter service-template record is malformed"
  [[ -f "${TARGET_LOCK}" && ! -L "${TARGET_LOCK}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${TARGET_LOCK}")" == 0:0:600:1 ]] || die "unsafe waiter target lock"
  [[ -f "${ACTIVE_REGISTRATION}" && ! -L "${ACTIVE_REGISTRATION}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${ACTIVE_REGISTRATION}")" == 0:0:600:1 &&
    "$(kv_value schema "${ACTIVE_REGISTRATION}")" == commu-matrix-waiter-active-v1 &&
    "$(kv_value target_id "${ACTIVE_REGISTRATION}")" == "${TARGET_ID}" &&
    "$(kv_value record_id "${ACTIVE_REGISTRATION}")" == "${RECORD_ID}" ]] || die "unsafe waiter active registration"
  regular_root_file "${PLAN}" && [[ "$(/usr/bin/stat -c %a -- "${PLAN}")" == 444 &&
    "$(sha256_file "${PLAN}")" == "${PLAN_SHA256}" ]] || die "immutable run plan changed after queueing"
}

remove_active_registration_locked() {
  if [[ ! -e "${ACTIVE_REGISTRATION}" && ! -L "${ACTIVE_REGISTRATION}" ]]; then
    return 0
  fi
  [[ -f "${ACTIVE_REGISTRATION}" && ! -L "${ACTIVE_REGISTRATION}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${ACTIVE_REGISTRATION}")" == 0:0:600:1 ]] || return 1
  [[ "$(kv_value schema "${ACTIVE_REGISTRATION}" 2>/dev/null)" == commu-matrix-waiter-active-v1 &&
    "$(kv_value target_id "${ACTIVE_REGISTRATION}" 2>/dev/null)" == "${TARGET_ID}" &&
    "$(kv_value record_id "${ACTIVE_REGISTRATION}" 2>/dev/null)" == "${RECORD_ID}" ]] || return 1
  /usr/bin/rm -- "${ACTIVE_REGISTRATION}"
}

gpu_is_free() {
  local inventory process_rows
  inventory="$(/usr/bin/timeout --signal=TERM --kill-after=2s 10s /usr/bin/nvidia-smi \
    -i "${GPU_INDEX}" --query-gpu=uuid --format=csv,noheader,nounits 2>/dev/null)" ||
    die "GPU inventory query failed or timed out"
  [[ "${inventory}" == "${GPU_UUID}" ]] || die "physical GPU UUID differs from queued immutable identity"
  process_rows="$(/usr/bin/timeout --signal=TERM --kill-after=2s 10s /usr/bin/nvidia-smi \
    --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null)" ||
    die "GPU process query failed or timed out"
  if /usr/bin/awk -F, -v wanted="${GPU_UUID}" '
    {gsub(/[[:space:]]/, "", $1); if ($1 == wanted) found=1}
    END {exit found ? 0 : 1}
  ' <<<"${process_rows}"; then
    return 1
  fi
  return 0
}

shared_runtime_is_free() {
  local tcp_listeners udp_listeners netns_rows
  tcp_listeners="$(/usr/bin/ss -H -lnt '( sport = :8000 or sport = :8001 or sport = :8443 )')" ||
    die "TCP listener query failed while waiting"
  udp_listeners="$(/usr/bin/ss -H -lnu '( sport = :8444 )')" ||
    die "UDP listener query failed while waiting"
  netns_rows="$(/usr/bin/ip netns list)" || die "network namespace query failed while waiting"
  [[ -z "${tcp_listeners}" && -z "${udp_listeners}" ]] || return 1
  if /usr/bin/grep -Eq '^llm-client([[:space:]]|$)' <<<"${netns_rows}"; then
    return 1
  fi
  return 0
}

wait_main() {
  local id="$1" now status_json deadline_fields deadline_now lease_minutes cleanup_epoch hard_deadline_epoch
  local target_lock_acquired=0 launch_was_submitted=0
  local -a launch_args
  load_wait_record "${id}"
  exec >>"${WAIT_LOG}" 2>&1
  wait_cleanup() {
    local rc=$? current_outcome=""
    trap - EXIT INT TERM HUP
    current_outcome="$(kv_value state "${OUTCOME}" 2>/dev/null || true)"
    if (( rc != 0 )) && [[ ! "${current_outcome}" =~ ^(submitted|not_needed|expired_without_launch)$ ]]; then
      set_wait_outcome failed || true
    fi
    if (( target_lock_acquired == 1 && launch_was_submitted == 0 )); then
      remove_active_registration_locked || rc=1
    fi
    /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
    /usr/bin/date '+WAIT_RESUME_EXIT %F %T %z epoch=%s'
    exit "${rc}"
  }
  trap wait_cleanup EXIT
  trap 'exit 130' INT TERM HUP
  exec 8<>"${TARGET_LOCK}" || die "cannot open waiter target lock"
  /usr/bin/flock -n 8 || die "another waiter already owns this immutable run target"
  target_lock_acquired=1
  set_wait_outcome waiting || die "could not record waiter start"
  /usr/bin/date '+WAIT_RESUME_START %F %T %z epoch=%s'
  printf 'run_id=%s gpu_index=%s gpu_uuid=%s cutoff_epoch=%s\n' \
    "${RUN_ID}" "${GPU_INDEX}" "${GPU_UUID}" "${AUTHORIZATION_CUTOFF_EPOCH}"

  while true; do
    now="$(/usr/bin/date +%s)"
    if (( AUTHORIZATION_CUTOFF_EPOCH - now < 20 * 60 )); then
      set_wait_outcome expired_without_launch || die "could not record waiter expiry"
      printf 'WAIT_RESUME_EXPIRED_WITHOUT_LAUNCH now_epoch=%s cutoff_epoch=%s\n' \
        "${now}" "${AUTHORIZATION_CUTOFF_EPOCH}"
      exit 0
    fi
    if ! gpu_is_free; then
      printf 'WAIT_RESUME_GPU_OCCUPIED epoch=%s gpu_index=%s\n' "${now}" "${GPU_INDEX}"
      /usr/bin/sleep "${POLL_SECONDS}"
      continue
    fi
    if ! shared_runtime_is_free; then
      printf 'WAIT_RESUME_SHARED_RUNTIME_OCCUPIED epoch=%s\n' "${now}"
      /usr/bin/sleep "${POLL_SECONDS}"
      continue
    fi

    status_json="$(/usr/bin/python3 -I "${STATE_TOOL}" status --root "${MATRIX_ROOT}")" ||
      die "immutable matrix state validation failed while GPU was free"
    if [[ "${status_json}" == *'"state": "complete"'* ]]; then
      set_wait_outcome not_needed || die "could not record completed outcome"
      printf 'WAIT_RESUME_NOT_NEEDED matrix_state=complete\n'
      exit 0
    fi
    [[ "${status_json}" == *'"state": "in-progress"'* ]] || die "matrix state is not resumable"

    now="$(/usr/bin/date +%s)"
    deadline_fields="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" deadline \
      --now "${now}" --lease "${MAX_LEASE}" \
      --authorization-cutoff "${AUTHORIZATION_CUTOFF_EPOCH}")" || {
      printf 'WAIT_RESUME_EXPIRED_WITHOUT_LAUNCH now_epoch=%s cutoff_epoch=%s\n' \
        "${now}" "${AUTHORIZATION_CUTOFF_EPOCH}"
      set_wait_outcome expired_without_launch || die "could not record waiter expiry"
      exit 0
    }
    IFS=$'\t' read -r deadline_now lease_minutes cleanup_epoch hard_deadline_epoch <<<"${deadline_fields}"
    [[ "${deadline_now}" == "${now}" && "${lease_minutes}" =~ ^[0-9]+$ &&
      "${cleanup_epoch}" =~ ^[0-9]+$ && "${hard_deadline_epoch}" =~ ^[0-9]+$ &&
      "${hard_deadline_epoch}" -le "${AUTHORIZATION_CUTOFF_EPOCH}" ]] ||
      die "bounded deadline helper returned malformed output"

    launch_args=(resume --run-id "${RUN_ID}" --source-repository-sha "${SOURCE_REPOSITORY_SHA}"
      --gpu-index "${GPU_INDEX}" --lease "${lease_minutes}m"
      --authorization-cutoff-epoch "${AUTHORIZATION_CUTOFF_EPOCH}")
    if [[ "${SERVICE_CONFIG_TEMPLATE}" != none ]]; then
      launch_args+=(--service-config-template "${SERVICE_CONFIG_TEMPLATE}")
    fi
    printf 'WAIT_RESUME_GPU_FREE epoch=%s lease_minutes=%s hard_deadline_epoch=%s\n' \
      "${now}" "${lease_minutes}" "${hard_deadline_epoch}"
    set_wait_outcome launching || die "could not record launch attempt"
    if "${LAUNCHER}" "${launch_args[@]}"; then
      launch_was_submitted=1
      set_wait_outcome submitted || die "could not record submitted launch"
      printf 'WAIT_RESUME_LAUNCH_SUBMITTED\n'
      exit 0
    fi
    die "authoritative matrix resume launch failed; it was not retried"
  done
}

expire_wait_main() {
  local id="$1" current_outcome=""
  load_expiry_record "${id}"
  exec >>"${WAIT_LOG}" 2>&1
  /usr/bin/date '+WAIT_RESUME_EXPIRY_START %F %T %z epoch=%s'
  if /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
    /usr/bin/tmux send-keys -t "${SESSION}" C-c
    for _ in $(/usr/bin/seq 1 30); do
      /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null || break
      /usr/bin/sleep 1
    done
  fi
  if /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
    /usr/bin/tmux kill-session -t "${SESSION}"
  fi
  /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null && die "waiter tmux remains after expiry"
  current_outcome="$(kv_value state "${OUTCOME}" 2>/dev/null || true)"
  if [[ ! "${current_outcome}" =~ ^(submitted|not_needed)$ ]]; then
    set_wait_outcome expired_without_launch || die "could not record expiry outcome"
  fi
  /usr/bin/date '+WAIT_RESUME_EXPIRY_OK %F %T %z epoch=%s'
}

ACTION="${1:-}"
[[ -n "${ACTION}" ]] && shift || true
if [[ "${ACTION}" == _expire-wait ]]; then
  [[ $# -eq 2 && "$1" == --record-id && "$2" =~ ^[0-9a-f]{16}$ ]] || die "invalid internal expiry invocation"
  expire_wait_main "$2"
  exit
fi
release_precheck
case "${ACTION}" in
  _wait)
    [[ $# -eq 2 && "$1" == --record-id && "$2" =~ ^[0-9a-f]{16}$ ]] || die "invalid internal invocation"
    wait_main "$2"
    exit
    ;;
  wait-resume) ;;
  *) usage ;;
esac

RUN_ID=""; SOURCE_REPOSITORY_SHA=""; GPU_INDEX=""; AUTHORIZATION_CUTOFF_EPOCH=""
MAX_LEASE=110m; SERVICE_CONFIG_TEMPLATE=none; MAX_LEASE_SET=0; SERVICE_CONFIG_SET=0
while (($#)); do
  case "$1" in
    --run-id) [[ $# -ge 2 && -z "${RUN_ID}" ]] || usage; RUN_ID="$2"; shift 2 ;;
    --source-repository-sha) [[ $# -ge 2 && -z "${SOURCE_REPOSITORY_SHA}" ]] || usage; SOURCE_REPOSITORY_SHA="$2"; shift 2 ;;
    --gpu-index) [[ $# -ge 2 && -z "${GPU_INDEX}" ]] || usage; GPU_INDEX="$2"; shift 2 ;;
    --authorization-cutoff-epoch) [[ $# -ge 2 && -z "${AUTHORIZATION_CUTOFF_EPOCH}" ]] || usage; AUTHORIZATION_CUTOFF_EPOCH="$2"; shift 2 ;;
    --max-lease) [[ $# -ge 2 && "${MAX_LEASE_SET}" -eq 0 ]] || usage; MAX_LEASE="$2"; MAX_LEASE_SET=1; shift 2 ;;
    --service-config-template) [[ $# -ge 2 && "${SERVICE_CONFIG_SET}" -eq 0 ]] || usage; SERVICE_CONFIG_TEMPLATE="$2"; SERVICE_CONFIG_SET=1; shift 2 ;;
    *) usage ;;
  esac
done
[[ "${RUN_ID}" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ && "${SOURCE_REPOSITORY_SHA}" =~ ^[0-9a-f]{40}$ &&
  "${GPU_INDEX}" =~ ^(0|[1-9][0-9]*)$ && "${AUTHORIZATION_CUTOFF_EPOCH}" =~ ^(0|[1-9][0-9]*)$ &&
  "${MAX_LEASE}" =~ ^(0|[1-9][0-9]*)(m|h)?$ ]] || usage
[[ "${SERVICE_CONFIG_TEMPLATE}" == none ||
  ( "${SERVICE_CONFIG_TEMPLATE}" = /* && "${SERVICE_CONFIG_TEMPLATE}" != *$'\n'* ) ]] || usage

NOW_EPOCH="$(/usr/bin/date +%s)"
/usr/bin/python3 -I "${LAUNCH_HELPER}" deadline --now "${NOW_EPOCH}" --lease "${MAX_LEASE}" \
  --authorization-cutoff "${AUTHORIZATION_CUTOFF_EPOCH}" >/dev/null ||
  die "authorization window must leave 20 minutes and end no more than two hours from now"
GPU_UUID="$(/usr/bin/timeout --signal=TERM --kill-after=2s 10s /usr/bin/nvidia-smi \
  -i "${GPU_INDEX}" --query-gpu=uuid --format=csv,noheader,nounits 2>/dev/null)" ||
  die "GPU inventory query failed or timed out"
[[ "${GPU_UUID}" =~ ^GPU-[0-9A-Fa-f-]+$ ]] || die "GPU inventory returned an unsafe UUID"
PLAN="/var/lib/commu-secure-matrix/${SOURCE_REPOSITORY_SHA}/gpu-${GPU_INDEX}-${GPU_UUID}/runs/${RUN_ID}/RUN_PLAN.json"
regular_root_file "${PLAN}" && [[ "$(/usr/bin/stat -c %a -- "${PLAN}")" == 444 ]] ||
  die "the asserted run has no immutable RUN_PLAN.json on GPU ${GPU_INDEX}"
identity_json="$(/usr/bin/python3 -I "${LAUNCH_HELPER}" resume-identity --plan "${PLAN}" \
  --gpu-index "${GPU_INDEX}" --gpu-uuid-output "${GPU_UUID}")" || die "could not validate immutable run identity"
mapfile -d '' -t identity_fields < <(/usr/bin/python3 -I - "${identity_json}" <<'PY'
import json, sys
identity = json.loads(sys.argv[1])
for name in ("repository_sha", "run_id", "gpu_index", "gpu_uuid", "run_plan_sha256"):
    sys.stdout.buffer.write(str(identity[name]).encode("ascii") + b"\0")
PY
)
[[ "${#identity_fields[@]}" -eq 5 && "${identity_fields[0]}" == "${SOURCE_REPOSITORY_SHA}" &&
  "${identity_fields[1]}" == "${RUN_ID}" && "${identity_fields[2]}" == "${GPU_INDEX}" &&
  "${identity_fields[3]}" == "${GPU_UUID}" ]] || die "run-plan identity differs from queue assertions"
PLAN_SHA256="${identity_fields[4]}"

if [[ "${SERVICE_CONFIG_TEMPLATE}" != none ]]; then
  SERVICE_CONFIG_TEMPLATE="$(/usr/bin/readlink -e -- "${SERVICE_CONFIG_TEMPLATE}")" || die "service template does not exist"
  [[ -f "${SERVICE_CONFIG_TEMPLATE}" && ! -L "${SERVICE_CONFIG_TEMPLATE}" ]] || die "service template is not a regular file"
fi

/usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${WAITERS_ROOT}"
[[ -d "${WAITERS_ROOT}" && ! -L "${WAITERS_ROOT}" &&
  "$(/usr/bin/stat -c %u:%g:%a -- "${WAITERS_ROOT}")" == "0:${SERVICE_GID}:710" ]] || die "unsafe waiter records root"
if [[ ! -e "${WAIT_LOCK_ROOT}" && ! -L "${WAIT_LOCK_ROOT}" ]]; then
  /usr/bin/mkdir --mode=0755 -- "${WAIT_LOCK_ROOT}" 2>/dev/null || true
fi
trusted_root_dir "${WAIT_LOCK_ROOT}" || die "unsafe waiter lock root"
TARGET_ID="$(printf '%s\0%s\0%s\0%s\0' "${SOURCE_REPOSITORY_SHA}" "${RUN_ID}" "${GPU_INDEX}" "${GPU_UUID}" |
  /usr/bin/sha256sum | /usr/bin/awk '{print substr($1, 1, 16)}')"
[[ "${TARGET_ID}" =~ ^[0-9a-f]{16}$ ]] || die "could not derive waiter target identity"
TARGET_LOCK="${WAIT_LOCK_ROOT}/target-${TARGET_ID}.lock"
if [[ ! -e "${TARGET_LOCK}" && ! -L "${TARGET_LOCK}" ]]; then
  if ! (set -o noclobber; : >"${TARGET_LOCK}") 2>/dev/null; then
    [[ -e "${TARGET_LOCK}" || -L "${TARGET_LOCK}" ]] || die "could not publish waiter target lock"
  fi
fi
[[ -f "${TARGET_LOCK}" && ! -L "${TARGET_LOCK}" &&
  "$(/usr/bin/stat -c %u:%g:%a:%h -- "${TARGET_LOCK}")" == 0:0:600:1 ]] || die "unsafe waiter target lock"
ACTIVE_REGISTRATION="${WAIT_LOCK_ROOT}/active-${TARGET_ID}.state"

RECORD_DIR=""
for _ in $(/usr/bin/seq 1 100); do
  RECORD_ID="$(/usr/bin/openssl rand -hex 8)"
  [[ "${RECORD_ID}" =~ ^[0-9a-f]{16}$ ]] || continue
  RECORD_DIR="${WAITERS_ROOT}/${RECORD_ID}"
  /usr/bin/mkdir --mode=0700 -- "${RECORD_DIR}" 2>/dev/null && break
  RECORD_DIR=""
done
[[ -n "${RECORD_DIR}" ]] || die "cannot allocate a unique waiter record"
/usr/bin/chown root:"${SERVICE_GID}" "${RECORD_DIR}"
/usr/bin/chmod 0710 "${RECORD_DIR}"
SESSION="commu-matrix-wait-${RECORD_ID}"
TIMER_BASE="${SESSION}-expiry"
WAIT_LOG="${RECORD_DIR}/wait.log"
OUTCOME="${RECORD_DIR}/outcome.state"
RECORD="${RECORD_DIR}/record.state"
{
  printf 'schema=commu-matrix-waiter-record-v1\nrecord_id=%s\ntarget_id=%s\nsession=%s\ntimer_base=%s\n' \
    "${RECORD_ID}" "${TARGET_ID}" "${SESSION}" "${TIMER_BASE}"
  printf 'release_repository_sha=%s\nsource_repository_sha=%s\nrun_id=%s\n' \
    "${REPOSITORY_SHA}" "${SOURCE_REPOSITORY_SHA}" "${RUN_ID}"
  printf 'gpu_index=%s\ngpu_uuid=%s\nplan=%s\nplan_sha256=%s\n' \
    "${GPU_INDEX}" "${GPU_UUID}" "${PLAN}" "${PLAN_SHA256}"
  printf 'authorization_cutoff_epoch=%s\nmax_lease=%s\nservice_config_template=%s\n' \
    "${AUTHORIZATION_CUTOFF_EPOCH}" "${MAX_LEASE}" "${SERVICE_CONFIG_TEMPLATE}"
  printf 'target_lock=%s\nactive_registration=%s\ncreated_epoch=%s\n' \
    "${TARGET_LOCK}" "${ACTIVE_REGISTRATION}" "${NOW_EPOCH}"
} >"${RECORD}"
/usr/bin/chown root:root "${RECORD}"
/usr/bin/chmod 0600 "${RECORD}"
/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${WAIT_LOG}"
/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${OUTCOME}"
printf 'state=queued\nupdated_epoch=%s\n' "${NOW_EPOCH}" >"${OUTCOME}"

exec 9<>"${TARGET_LOCK}" || die "cannot open waiter target lock for registration"
/usr/bin/flock -n 9 || die "another submission is registering this immutable run target"
if [[ -e "${ACTIVE_REGISTRATION}" || -L "${ACTIVE_REGISTRATION}" ]]; then
  [[ -f "${ACTIVE_REGISTRATION}" && ! -L "${ACTIVE_REGISTRATION}" &&
    "$(/usr/bin/stat -c %u:%g:%a:%h -- "${ACTIVE_REGISTRATION}")" == 0:0:600:1 ]] ||
    die "unsafe existing waiter registration"
  old_schema="$(kv_value schema "${ACTIVE_REGISTRATION}" 2>/dev/null || true)"
  old_target_id="$(kv_value target_id "${ACTIVE_REGISTRATION}" 2>/dev/null || true)"
  old_record_id="$(kv_value record_id "${ACTIVE_REGISTRATION}" 2>/dev/null || true)"
  old_session="$(kv_value session "${ACTIVE_REGISTRATION}" 2>/dev/null || true)"
  old_cutoff="$(kv_value authorization_cutoff_epoch "${ACTIVE_REGISTRATION}" 2>/dev/null || true)"
  [[ "${old_schema}" == commu-matrix-waiter-active-v1 && "${old_target_id}" == "${TARGET_ID}" &&
    "${old_record_id}" =~ ^[0-9a-f]{16}$ && "${old_session}" == "commu-matrix-wait-${old_record_id}" &&
    "${old_cutoff}" =~ ^(0|[1-9][0-9]*)$ ]] || die "existing waiter registration is malformed"
  if (( old_cutoff >= NOW_EPOCH )) || /usr/bin/tmux has-session -t "${old_session}" 2>/dev/null; then
    die "this immutable run target already has a bounded waiter registration"
  fi
fi
registration_tmp="${ACTIVE_REGISTRATION}.tmp.$$"
[[ ! -e "${registration_tmp}" && ! -L "${registration_tmp}" ]] || die "waiter registration temporary path is occupied"
/usr/bin/install -o root -g root -m 0600 /dev/null "${registration_tmp}"
{
  printf 'schema=commu-matrix-waiter-active-v1\ntarget_id=%s\nrecord_id=%s\nsession=%s\n' \
    "${TARGET_ID}" "${RECORD_ID}" "${SESSION}"
  printf 'authorization_cutoff_epoch=%s\n' "${AUTHORIZATION_CUTOFF_EPOCH}"
} >"${registration_tmp}"
/usr/bin/mv -- "${registration_tmp}" "${ACTIVE_REGISTRATION}"
/usr/bin/flock -u 9 || die "could not release waiter registration lock"
exec 9>&-

registration_owned=1
timer_armed=0
public_cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  if (( timer_armed == 1 )); then
    /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
  fi
  if (( registration_owned == 1 )); then
    exec 9<>"${TARGET_LOCK}" || true
    if /usr/bin/flock -n 9 2>/dev/null; then
      remove_active_registration_locked || true
      /usr/bin/flock -u 9 >/dev/null 2>&1 || true
    fi
    exec 9>&- || true
  fi
  exit "${rc}"
}
trap public_cleanup EXIT
trap 'exit 130' INT TERM HUP

TIMER_ARM_EPOCH="$(/usr/bin/date +%s)"
DELAY_SECONDS=$((AUTHORIZATION_CUTOFF_EPOCH - TIMER_ARM_EPOCH))
(( DELAY_SECONDS >= 20 * 60 )) || die "not enough authorization time remains to queue the waiter"
if ! /usr/bin/systemd-run --unit="${TIMER_BASE}" --on-active="${DELAY_SECONDS}s" \
  --timer-property=AccuracySec=1s /usr/bin/bash -p "${SELF}" _expire-wait --record-id "${RECORD_ID}"; then
  die "could not arm independent waiter expiry"
fi
timer_armed=1
if ! /usr/bin/tmux new-session -d -s "${SESSION}" "${SELF} _wait --record-id ${RECORD_ID}"; then
  die "could not create the waiter tmux session"
fi
/usr/bin/sleep 2
if ! /usr/bin/tmux has-session -t "${SESSION}" 2>/dev/null; then
  early_outcome="$(kv_value state "${OUTCOME}" 2>/dev/null || true)"
  /usr/bin/systemctl stop "${TIMER_BASE}.timer" >/dev/null 2>&1 || true
  case "${early_outcome}" in
    submitted|not_needed|expired_without_launch)
      registration_owned=0
      timer_armed=0
      trap - EXIT INT TERM HUP
      printf 'MATRIX_WAIT_RESUME_FINISHED_EARLY outcome=%s wait_log=%s\n' "${early_outcome}" "${WAIT_LOG}"
      exit 0
      ;;
    *) die "waiter exited immediately with outcome ${early_outcome:-unknown}; inspect ${WAIT_LOG}" ;;
  esac
fi

registration_owned=0
timer_armed=0
trap - EXIT INT TERM HUP

printf 'MATRIX_WAIT_RESUME_SUBMITTED session=%s record=%s\n' "${SESSION}" "${RECORD}"
printf 'run_id=%s gpu_index=%s gpu_uuid=%s poll_seconds=%s\n' \
  "${RUN_ID}" "${GPU_INDEX}" "${GPU_UUID}" "${POLL_SECONDS}"
printf 'authorization_cutoff_local=%s authorization_cutoff_epoch=%s\n' \
  "$(/usr/bin/date -d "@${AUTHORIZATION_CUTOFF_EPOCH}" '+%F %T %z')" "${AUTHORIZATION_CUTOFF_EPOCH}"
printf 'wait_log=%s\n' "${WAIT_LOG}"
