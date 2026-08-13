#!/usr/bin/env bash
set -euo pipefail
set +x

# Safely replace a user-owned vLLM controller with a one- or two-worker
# topology. The credential is recovered from the verified controller and is
# kept only in shell/process memory; it is never passed in argv or persisted.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LAUNCHER="${SCRIPT_DIR}/03_start_vllm_dual.sh"
ACTION="${1:-}"
[[ -n "${ACTION}" ]] && shift || true

STATE_FILE="${VLLM_ACTIVE_STATE_FILE:-}"
TARGET_ENV="${VLLM_TARGET_ENV_FILE:-}"
LOG_FILE="${VLLM_SWITCH_LOG_FILE:-}"
START_TIMEOUT="${VLLM_SWITCH_TIMEOUT_SECONDS:-300}"

usage() {
  cat <<'EOF'
Usage:
  24_switch_vllm_topology.sh switch --state PATH --target-env PATH [--log PATH] [--timeout SECONDS]
  24_switch_vllm_topology.sh check --state PATH [--timeout SECONDS]
  24_switch_vllm_topology.sh validate-config --target-env PATH

The state and target configuration must be explicit flags or the corresponding
VLLM_ACTIVE_STATE_FILE/VLLM_TARGET_ENV_FILE environment variables.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

while (($#)); do
  case "$1" in
    --state) [[ $# -ge 2 ]] || die "--state requires a path"; STATE_FILE="$2"; shift 2 ;;
    --target-env) [[ $# -ge 2 ]] || die "--target-env requires a path"; TARGET_ENV="$2"; shift 2 ;;
    --log) [[ $# -ge 2 ]] || die "--log requires a path"; LOG_FILE="$2"; shift 2 ;;
    --timeout) [[ $# -ge 2 ]] || die "--timeout requires seconds"; START_TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${ACTION}" == "switch" || "${ACTION}" == "check" || "${ACTION}" == "validate-config" ]] || {
  usage >&2
  exit 2
}
[[ "${START_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "timeout must be a positive integer"

canonical_file() {
  local path="$1" resolved
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  resolved="$(readlink -e -- "${path}")" || return 1
  printf '%s\n' "${resolved}"
}

sha256_file() { sha256sum -- "$1" | awk '{print $1}'; }

state_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}' "${STATE_FILE}"
}

proc_stat_tail() {
  local pid="$1" line
  [[ "${pid}" =~ ^[0-9]+$ && -r "/proc/${pid}/stat" ]] || return 1
  IFS= read -r line <"/proc/${pid}/stat" || return 1
  [[ "${line}" == *") "* ]] || return 1
  printf '%s\n' "${line##*) }"
}

proc_field() { local tail; tail="$(proc_stat_tail "$1")" || return 1; awk -v n="$2" '{print $n}' <<<"${tail}"; }
proc_state() { proc_field "$1" 1; }
proc_ppid() { proc_field "$1" 2; }
proc_pgid() { proc_field "$1" 3; }
proc_sid() { proc_field "$1" 4; }
proc_ticks() { proc_field "$1" 20; }
proc_uid() { awk '/^Uid:/ {print $2; exit}' "/proc/$1/status" 2>/dev/null; }
proc_cmdline() { tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null; }
proc_env() { tr '\0' '\n' <"/proc/$1/environ" 2>/dev/null; }
proc_env_value() { proc_env "$1" | sed -n "s/^$2=//p" | head -n 1; }

is_descendant() {
  local pid="$1" ancestor="$2" parent steps=0
  while [[ "${pid}" =~ ^[0-9]+$ && "${pid}" -gt 1 && "${steps}" -lt 128 ]]; do
    [[ "${pid}" == "${ancestor}" ]] && return 0
    parent="$(proc_ppid "${pid}" 2>/dev/null || true)"
    [[ "${parent}" =~ ^[0-9]+$ && "${parent}" != "${pid}" ]] || return 1
    pid="${parent}"
    ((steps += 1))
  done
  return 1
}

listener_pid() {
  local port="$1" rows pids
  rows="$(ss -H -ltnp "sport = :${port}" 2>/dev/null)" || return 1
  [[ -n "${rows}" ]] || return 1
  grep -Eq "127\\.0\\.0\\.1:${port}[[:space:]]" <<<"${rows}" || return 1
  pids="$(grep -oE 'pid=[0-9]+' <<<"${rows}" | cut -d= -f2 | sort -u)"
  [[ "$(wc -w <<<"${pids}")" -eq 1 ]] || return 1
  printf '%s\n' "${pids}"
}

port_closed() { [[ -z "$(ss -H -ltn "sport = :$1" 2>/dev/null)" ]]; }

CFG_WORKERS="" CFG_GPU_CSV="" CFG_HOST="" CFG_PORT="" CFG_STEP=""
CFG_MODEL="" CFG_REVISION="" CFG_RUNS_ROOT="" CFG_PATH="" CFG_SHA=""
declare -a CFG_GPUS=() CFG_PORTS=() CFG_UUIDS=()

load_config() {
  local requested="$1" resolve_gpus="${2:-true}" gpu index uuid rows
  CFG_PATH="$(canonical_file "${requested}")" || die "configuration must be a regular, non-symlink file: ${requested}"
  if grep -Eq '^[[:space:]]*(export[[:space:]]+)?(LOCAL_VLLM_API_KEY|VLLM_API_KEY|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)[[:space:]]*=' "${CFG_PATH}"; then
    die "credential assignment found in configuration"
  fi
  unset PARALLEL_WORKERS CUDA_VISIBLE_DEVICES VLLM_HOST VLLM_PORT VLLM_PORT_STEP
  unset VLLM_MODEL VLLM_MODEL_REVISION RUNS_ROOT
  # shellcheck disable=SC1090
  source "${CFG_PATH}"
  CFG_WORKERS="${PARALLEL_WORKERS:-2}"
  CFG_GPU_CSV="${CUDA_VISIBLE_DEVICES:-}"
  CFG_HOST="${VLLM_HOST:-}"
  CFG_PORT="${VLLM_PORT:-}"
  CFG_STEP="${VLLM_PORT_STEP:-1}"
  CFG_MODEL="${VLLM_MODEL:-}"
  CFG_REVISION="${VLLM_MODEL_REVISION:-}"
  CFG_RUNS_ROOT="${RUNS_ROOT:-}"
  CFG_SHA="$(sha256_file "${CFG_PATH}")"
  [[ "${CFG_WORKERS}" == "1" || "${CFG_WORKERS}" == "2" ]] || die "PARALLEL_WORKERS must be 1 or 2"
  [[ "${CFG_HOST}" == "127.0.0.1" ]] || die "VLLM_HOST must be 127.0.0.1"
  [[ "${CFG_PORT}" == "8000" && "${CFG_STEP}" == "1" ]] || die "worker ports must be 8000 and, in dual mode, 8001"
  [[ -n "${CFG_MODEL}" && -n "${CFG_RUNS_ROOT}" ]] || die "model and RUNS_ROOT must be non-empty"
  IFS=',' read -r -a CFG_GPUS <<<"${CFG_GPU_CSV}"
  [[ "${#CFG_GPUS[@]}" -eq "${CFG_WORKERS}" ]] || die "CUDA_VISIBLE_DEVICES must list exactly one GPU per worker"
  [[ "${CFG_GPU_CSV}" != *[[:space:]]* ]] || die "CUDA_VISIBLE_DEVICES must not contain whitespace"
  [[ "${CFG_GPUS[0]}" =~ ^[0-9]+$ ]] || die "GPU indices must be non-negative integers"
  if [[ "${CFG_WORKERS}" == "2" ]]; then
    [[ "${CFG_GPUS[1]}" =~ ^[0-9]+$ && "${CFG_GPUS[0]}" != "${CFG_GPUS[1]}" ]] || die "dual mode requires two distinct GPU indices"
  fi
  CFG_PORTS=(); for ((index=0; index<CFG_WORKERS; index++)); do CFG_PORTS+=("$((CFG_PORT + index * CFG_STEP))"); done
  CFG_UUIDS=()
  if [[ "${resolve_gpus}" == "true" ]]; then
    command -v nvidia-smi >/dev/null 2>&1 || die "required command not found: nvidia-smi"
    rows="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits)" || die "cannot query GPU identities"
    for gpu in "${CFG_GPUS[@]}"; do
      uuid="$(awk -F',' -v wanted="${gpu}" '{gsub(/ /, "", $1); gsub(/^ +| +$/, "", $2); if ($1 == wanted) print $2}' <<<"${rows}")"
      [[ -n "${uuid}" && "$(wc -w <<<"${uuid}")" -eq 1 ]] || die "GPU ${gpu} does not resolve to exactly one UUID"
      CFG_UUIDS+=("${uuid}")
    done
  fi
}

validate_controller() {
  local pid="$1" ticks="$2" config="$3" expected_uid="$4" cmd env_config
  [[ "${pid}" =~ ^[0-9]+$ && "${ticks}" =~ ^[0-9]+$ ]] || return 1
  [[ "$(proc_ticks "${pid}" 2>/dev/null)" == "${ticks}" && "$(proc_state "${pid}" 2>/dev/null)" != "Z" ]] || return 1
  [[ "$(proc_uid "${pid}")" == "${expected_uid}" && "${expected_uid}" == "$(id -u)" ]] || return 1
  cmd="$(proc_cmdline "${pid}")"
  [[ "${cmd}" == *"03_start_vllm_dual.sh"* ]] || return 1
  env_config="$(proc_env_value "${pid}" EXPERIMENT_ENV_FILE)"
  [[ -n "${env_config}" ]] || return 1
  [[ "$(readlink -e -- "${env_config}" 2>/dev/null)" == "${config}" ]] || return 1
}

health_models() {
  local port="$1" api_key="$2"
  [[ "${api_key}" != *$'\n'* && "${api_key}" != *$'\r'* ]] || return 1
  printf 'Authorization: Bearer %s\n' "${api_key}" |
    curl --silent --show-error --fail --max-time 10 --header @- \
      --output /dev/null "http://127.0.0.1:${port}/v1/models"
}

gpu_engine_pid() {
  local uuid="$1" controller="$2" rows candidate found=""
  rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null)" || return 1
  while IFS=',' read -r candidate_uuid candidate; do
    candidate_uuid="${candidate_uuid//[[:space:]]/}"; candidate="${candidate//[[:space:]]/}"
    [[ "${candidate_uuid}" == "${uuid}" && "${candidate}" =~ ^[0-9]+$ ]] || continue
    is_descendant "${candidate}" "${controller}" || continue
    [[ -z "${found}" ]] || return 1
    found="${candidate}"
  done <<<"${rows}"
  [[ -n "${found}" ]] || return 1
  printf '%s\n' "${found}"
}

controller_gpu_processes_exact() {
  local controller="$1" rows candidate_uuid candidate configured_uuid expected count=0
  rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null)" || return 1
  while IFS=',' read -r candidate_uuid candidate; do
    candidate_uuid="${candidate_uuid//[[:space:]]/}"; candidate="${candidate//[[:space:]]/}"
    [[ "${candidate}" =~ ^[0-9]+$ ]] || continue
    is_descendant "${candidate}" "${controller}" || continue
    expected=false
    for configured_uuid in "${CFG_UUIDS[@]}"; do
      [[ "${candidate_uuid}" == "${configured_uuid}" ]] && expected=true
    done
    [[ "${expected}" == "true" ]] || return 1
    ((count += 1))
  done <<<"${rows}"
  [[ "${count}" -eq "${CFG_WORKERS}" ]]
}

VERIFIED_CONTROLLER_PID="" VERIFIED_CONTROLLER_TICKS="" VERIFIED_API_KEY=""
declare -a VERIFIED_API_PIDS=() VERIFIED_ENGINE_PIDS=() VERIFIED_API_TICKS=() VERIFIED_ENGINE_TICKS=()

verify_live_service() {
  local controller="$1" ticks="$2" config="$3" expected_uid="$4" recover_key="$5"
  local i api engine api_gpu api_cmd
  validate_controller "${controller}" "${ticks}" "${config}" "${expected_uid}" || return 1
  if [[ "${recover_key}" == "true" ]]; then
    VERIFIED_API_KEY="$(proc_env_value "${controller}" LOCAL_VLLM_API_KEY)"
    [[ -n "${VERIFIED_API_KEY}" ]] || return 1
  fi
  VERIFIED_API_PIDS=(); VERIFIED_ENGINE_PIDS=(); VERIFIED_API_TICKS=(); VERIFIED_ENGINE_TICKS=()
  for ((i=0; i<CFG_WORKERS; i++)); do
    api="$(listener_pid "${CFG_PORTS[i]}")" || return 1
    [[ "$(proc_uid "${api}")" == "${expected_uid}" ]] || return 1
    is_descendant "${api}" "${controller}" || return 1
    [[ "$(proc_pgid "${api}")" == "${api}" && "$(proc_sid "${api}")" == "${api}" ]] || return 1
    api_gpu="$(proc_env_value "${api}" CUDA_VISIBLE_DEVICES)"
    [[ "${api_gpu}" == "${CFG_GPUS[i]}" ]] || return 1
    api_cmd="$(proc_cmdline "${api}")"
    [[ "${api_cmd}" == *" serve "* && " ${api_cmd} " == *" --port ${CFG_PORTS[i]} "* && " ${api_cmd} " == *" ${CFG_MODEL} "* ]] || return 1
    if [[ -n "${CFG_REVISION}" ]]; then
      [[ " ${api_cmd} " == *" --revision ${CFG_REVISION} "* ]] || return 1
    fi
    engine="$(gpu_engine_pid "${CFG_UUIDS[i]}" "${controller}")" || return 1
    [[ "$(proc_uid "${engine}")" == "${expected_uid}" ]] || return 1
    VERIFIED_API_PIDS+=("${api}"); VERIFIED_ENGINE_PIDS+=("${engine}")
    VERIFIED_API_TICKS+=("$(proc_ticks "${api}")"); VERIFIED_ENGINE_TICKS+=("$(proc_ticks "${engine}")")
    health_models "${CFG_PORTS[i]}" "${VERIFIED_API_KEY}" || return 1
  done
  controller_gpu_processes_exact "${controller}" || return 1
  if [[ "${CFG_WORKERS}" == "1" ]]; then port_closed 8001 || return 1; fi
  VERIFIED_CONTROLLER_PID="${controller}"; VERIFIED_CONTROLLER_TICKS="${ticks}"
}

verify_recorded_service() {
  local schema config config_sha controller ticks uid i
  [[ -f "${STATE_FILE}" && ! -L "${STATE_FILE}" ]] || return 1
  [[ "$(stat -c %u -- "${STATE_FILE}")" == "$(id -u)" ]] || return 1
  schema="$(state_value schema)" || return 1
  [[ "${schema}" == "commu-vllm-service-state-v1" || "${schema}" == "commu-vllm-service-state-v2" ]] || return 1
  [[ "$(state_value status)" == "running" ]] || return 1
  config="$(state_value config)" || return 1
  config="$(canonical_file "${config}")" || return 1
  config_sha="$(state_value config_sha256)" || return 1
  [[ "$(sha256_file "${config}")" == "${config_sha}" ]] || return 1
  load_config "${config}"
  controller="$(state_value controller_pid)" || return 1
  ticks="$(state_value controller_start_ticks)" || return 1
  uid="$(state_value controller_uid 2>/dev/null || id -u)"
  verify_live_service "${controller}" "${ticks}" "${config}" "${uid}" true || return 1
  for ((i=0; i<CFG_WORKERS; i++)); do
    [[ "$(state_value "worker_${i}_port")" == "${CFG_PORTS[i]}" ]] || return 1
    [[ "$(state_value "worker_${i}_gpu_index")" == "${CFG_GPUS[i]}" ]] || return 1
    [[ "$(state_value "worker_${i}_gpu_uuid")" == "${CFG_UUIDS[i]}" ]] || return 1
    [[ "$(state_value "worker_${i}_api_pid")" == "${VERIFIED_API_PIDS[i]}" ]] || return 1
    [[ "$(state_value "worker_${i}_engine_pid")" == "${VERIFIED_ENGINE_PIDS[i]}" ]] || return 1
  done
}

wait_controller_exit() {
  local pid="$1" ticks="$2" attempt
  for ((attempt=0; attempt<START_TIMEOUT*10; attempt++)); do
    if [[ ! -r "/proc/${pid}/stat" || "$(proc_ticks "${pid}" 2>/dev/null)" != "${ticks}" || "$(proc_state "${pid}" 2>/dev/null)" == "Z" ]]; then return 0; fi
    sleep 0.1
  done
  return 1
}

stop_verified_controller() {
  local pid="$1" ticks="$2" config="$3" uid="$4" port
  validate_controller "${pid}" "${ticks}" "${config}" "${uid}" || return 1
  kill -TERM "${pid}" || return 1
  wait_controller_exit "${pid}" "${ticks}" || return 1
  for port in 8000 8001; do port_closed "${port}" || return 1; done
}

STARTED_PID="" STARTED_TICKS=""
start_service() {
  local config="$1" api_key="$2" log="$3" attempt
  [[ -x "${LAUNCHER}" ]] || return 1
  mkdir -p -- "$(dirname -- "${log}")"
  [[ ! -L "${log}" ]] || return 1
  : >>"${log}"; chmod 600 "${log}"
  (
    export EXPERIMENT_ENV_FILE="${config}"
    export LOCAL_VLLM_API_KEY="${api_key}"
    exec nohup setsid bash "${LAUNCHER}"
  ) >>"${log}" 2>&1 &
  STARTED_PID=$!
  for ((attempt=0; attempt<100; attempt++)); do
    STARTED_TICKS="$(proc_ticks "${STARTED_PID}" 2>/dev/null || true)"
    [[ -n "${STARTED_TICKS}" ]] && break
    kill -0 "${STARTED_PID}" 2>/dev/null || return 1
    sleep 0.01
  done
  [[ -n "${STARTED_TICKS}" ]] || return 1
}

wait_service_ready() {
  local pid="$1" ticks="$2" config="$3" uid="$4" api_key="$5" attempt
  VERIFIED_API_KEY="${api_key}"
  for ((attempt=0; attempt<START_TIMEOUT; attempt++)); do
    kill -0 "${pid}" 2>/dev/null || return 1
    if verify_live_service "${pid}" "${ticks}" "${config}" "${uid}" false; then return 0; fi
    sleep 1
  done
  return 1
}

write_state() {
  local config="$1" switch_result="$2" tmp i repo_sha
  tmp="$(mktemp "${STATE_FILE}.tmp.XXXXXX")" || return 1
  chmod 600 "${tmp}"
  repo_sha="$(git -C "${EXPERIMENT_ROOT}" rev-parse HEAD 2>/dev/null || printf unknown)"
  {
    printf 'schema=commu-vllm-service-state-v2\nstatus=running\n'
    printf 'repository_sha=%s\nconfig=%s\nconfig_sha256=%s\n' "${repo_sha}" "${config}" "$(sha256_file "${config}")"
    printf 'controller_pid=%s\ncontroller_start_ticks=%s\ncontroller_uid=%s\ncontroller_pgid=%s\ncontroller_sid=%s\n' \
      "${VERIFIED_CONTROLLER_PID}" "${VERIFIED_CONTROLLER_TICKS}" "$(id -u)" \
      "$(proc_pgid "${VERIFIED_CONTROLLER_PID}")" "$(proc_sid "${VERIFIED_CONTROLLER_PID}")"
    printf 'worker_count=%s\nswitch_result=%s\n' "${CFG_WORKERS}" "${switch_result}"
    for ((i=0; i<CFG_WORKERS; i++)); do
      printf 'worker_%s_port=%s\nworker_%s_gpu_index=%s\nworker_%s_gpu_uuid=%s\n' "$i" "${CFG_PORTS[i]}" "$i" "${CFG_GPUS[i]}" "$i" "${CFG_UUIDS[i]}"
      printf 'worker_%s_api_pid=%s\nworker_%s_api_start_ticks=%s\nworker_%s_engine_pid=%s\nworker_%s_engine_start_ticks=%s\n' \
        "$i" "${VERIFIED_API_PIDS[i]}" "$i" "${VERIFIED_API_TICKS[i]}" "$i" "${VERIFIED_ENGINE_PIDS[i]}" "$i" "${VERIFIED_ENGINE_TICKS[i]}"
    done
    printf 'started_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${tmp}"
  mv -f -- "${tmp}" "${STATE_FILE}"
}

if [[ "${ACTION}" == "validate-config" ]]; then
  [[ -n "${TARGET_ENV}" ]] || die "--target-env is required"
  load_config "${TARGET_ENV}" false
  printf 'VLLM_TOPOLOGY_CONFIG_OK workers=%s gpu_indices=%s ports=' "${CFG_WORKERS}" "${CFG_GPU_CSV}"
  (IFS=,; printf '%s\n' "${CFG_PORTS[*]}")
  exit 0
fi

[[ -n "${STATE_FILE}" ]] || die "--state is required"
[[ "${STATE_FILE}" = /* ]] || die "state path must be absolute"
STATE_DIR="$(dirname -- "${STATE_FILE}")"
[[ -d "${STATE_DIR}" && ! -L "${STATE_DIR}" ]] || die "state directory must exist and not be a symlink"
[[ ! -L "${STATE_FILE}" ]] || die "refusing symlinked state path"
[[ ! -L "${STATE_FILE}.lock" ]] || die "refusing symlinked state lock path"
for command_name in awk curl flock git nvidia-smi nohup readlink setsid sha256sum ss; do command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"; done
exec 9>"${STATE_FILE}.lock"
flock -n 9 || die "another topology operation holds ${STATE_FILE}.lock"

if ! verify_recorded_service; then die "active service state or live ownership/topology verification failed; nothing was signaled"; fi
if [[ "${ACTION}" == "check" ]]; then
  printf 'VLLM_TOPOLOGY_OK workers=%s gpu_indices=%s ports=' "${CFG_WORKERS}" "${CFG_GPU_CSV}"
  (IFS=,; printf '%s\n' "${CFG_PORTS[*]}")
  exit 0
fi

[[ -n "${TARGET_ENV}" ]] || die "--target-env is required"
PREVIOUS_CONFIG="${CFG_PATH}"; PREVIOUS_SHA="${CFG_SHA}"
PREVIOUS_CONTROLLER="${VERIFIED_CONTROLLER_PID}"; PREVIOUS_TICKS="${VERIFIED_CONTROLLER_TICKS}"
PREVIOUS_PGID="$(proc_pgid "${PREVIOUS_CONTROLLER}")"; PREVIOUS_SID="$(proc_sid "${PREVIOUS_CONTROLLER}")"
API_KEY="${VERIFIED_API_KEY}"
TARGET_CANON="$(canonical_file "${TARGET_ENV}")" || die "invalid target configuration"
load_config "${TARGET_CANON}"
TARGET_CONFIG="${CFG_PATH}"
TARGET_SHA="${CFG_SHA}"
[[ "${CFG_UUIDS[*]}" != "" ]] || die "target GPU identities were not resolved"
if [[ -z "${LOG_FILE}" ]]; then LOG_FILE="${STATE_DIR}/vllm-switch-$(date -u +%Y%m%dT%H%M%SZ).log"; fi
[[ "${LOG_FILE}" = /* && ! -L "${LOG_FILE}" ]] || die "log path must be absolute and not a symlink"

# Re-verify the exact previous controller immediately before the only signal.
load_config "${PREVIOUS_CONFIG}"
[[ "${CFG_SHA}" == "${PREVIOUS_SHA}" ]] || die "previous configuration changed during verification"
validate_controller "${PREVIOUS_CONTROLLER}" "${PREVIOUS_TICKS}" "${PREVIOUS_CONFIG}" "$(id -u)" || die "controller identity changed before stop"
[[ "$(proc_pgid "${PREVIOUS_CONTROLLER}")" == "${PREVIOUS_PGID}" && "$(proc_sid "${PREVIOUS_CONTROLLER}")" == "${PREVIOUS_SID}" ]] || die "controller session changed before stop"
stop_verified_controller "${PREVIOUS_CONTROLLER}" "${PREVIOUS_TICKS}" "${PREVIOUS_CONFIG}" "$(id -u)" || die "verified controller did not stop cleanly; state preserved"

load_config "${TARGET_CONFIG}"
[[ "${CFG_SHA}" == "${TARGET_SHA}" ]] || die "target configuration changed before start"
target_started=false
if start_service "${TARGET_CONFIG}" "${API_KEY}" "${LOG_FILE}" &&
  wait_service_ready "${STARTED_PID}" "${STARTED_TICKS}" "${TARGET_CONFIG}" "$(id -u)" "${API_KEY}"; then
  target_started=true
fi
if [[ "${target_started}" == "true" && "$(sha256_file "${TARGET_CONFIG}")" != "${TARGET_SHA}" ]]; then
  printf 'Target configuration changed during start; stopping the verified target and rolling back.\n' >&2
  stop_verified_controller "${STARTED_PID}" "${STARTED_TICKS}" "${TARGET_CONFIG}" "$(id -u)" || die "changed target configuration and verified target could not be stopped"
  target_started=false
fi
if [[ "${target_started}" == "true" ]]; then
  write_state "${TARGET_CONFIG}" switched || die "target is healthy but active state could not be written; inspect ${LOG_FILE}"
  unset API_KEY VERIFIED_API_KEY
  printf 'VLLM_TOPOLOGY_SWITCH_OK workers=%s gpu_indices=%s\n' "${CFG_WORKERS}" "${CFG_GPU_CSV}"
  exit 0
fi

printf 'Target topology failed health validation; attempting verified rollback. Diagnostics: %s\n' "${LOG_FILE}" >&2
if [[ -n "${STARTED_PID}" && -n "${STARTED_TICKS}" ]] && validate_controller "${STARTED_PID}" "${STARTED_TICKS}" "${TARGET_CONFIG}" "$(id -u)"; then
  stop_verified_controller "${STARTED_PID}" "${STARTED_TICKS}" "${TARGET_CONFIG}" "$(id -u)" || die "target failed and could not be stopped safely; stale prior state preserved"
fi
for port in 8000 8001; do port_closed "${port}" || die "target failed with a remaining listener on ${port}; rollback refused"; done

load_config "${PREVIOUS_CONFIG}"
[[ "${CFG_SHA}" == "${PREVIOUS_SHA}" ]] || die "previous configuration changed; rollback refused"
ROLLBACK_LOG="${LOG_FILE}.rollback"
if start_service "${PREVIOUS_CONFIG}" "${API_KEY}" "${ROLLBACK_LOG}" &&
  wait_service_ready "${STARTED_PID}" "${STARTED_TICKS}" "${PREVIOUS_CONFIG}" "$(id -u)" "${API_KEY}"; then
  write_state "${PREVIOUS_CONFIG}" rolled_back || die "rollback is healthy but active state could not be written"
  unset API_KEY VERIFIED_API_KEY
  printf 'ERROR: target failed; previous topology was restored. Diagnostics: %s\n' "${LOG_FILE}" >&2
  exit 1
fi
unset API_KEY VERIFIED_API_KEY
die "target and automatic rollback both failed; inspect ${LOG_FILE} and ${ROLLBACK_LOG}; no unverified process was signaled"
