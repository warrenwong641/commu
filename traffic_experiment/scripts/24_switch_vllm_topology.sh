#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

# Transactionally replace a user-owned vLLM controller. Configuration is
# parsed as inert data, credentials remain only in process memory, and only a
# controller whose complete recorded/live identity matches may be signalled.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LAUNCHER="${SCRIPT_DIR}/03_start_vllm_dual.sh"
ACTION="${1:-}"; [[ -n "${ACTION}" ]] && shift || true
STATE_FILE="${VLLM_ACTIVE_STATE_FILE:-}"
TARGET_ENV="${VLLM_TARGET_ENV_FILE:-}"
LOG_FILE="${VLLM_SWITCH_LOG_FILE:-}"
TIMEOUT="${VLLM_SWITCH_TIMEOUT_SECONDS:-300}"
PREV_FROZEN=""
TARGET_FROZEN=""
KEEP_FROZEN=""
LOCK_DIR=""

usage() {
  cat <<'EOF'
Usage:
  24_switch_vllm_topology.sh switch --state PATH --target-env PATH [--log PATH] [--timeout SECONDS]
  24_switch_vllm_topology.sh check --state PATH
  24_switch_vllm_topology.sh validate-config --target-env PATH
EOF
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
while (($#)); do
  case "$1" in
    --state) [[ $# -ge 2 ]] || die "--state requires a path"; STATE_FILE="$2"; shift 2 ;;
    --target-env) [[ $# -ge 2 ]] || die "--target-env requires a path"; TARGET_ENV="$2"; shift 2 ;;
    --log) [[ $# -ge 2 ]] || die "--log requires a path"; LOG_FILE="$2"; shift 2 ;;
    --timeout) [[ $# -ge 2 ]] || die "--timeout requires seconds"; TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "${ACTION}" == switch || "${ACTION}" == check || "${ACTION}" == validate-config ]] || { usage >&2; exit 2; }
[[ "${TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "timeout must be a positive integer"

canonical_regular() {
  local path="$1" resolved
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  [[ "$(stat -c %F -- "${path}" 2>/dev/null)" == "regular file" ]] || return 1
  resolved="$(readlink -e -- "${path}")" || return 1
  printf '%s\n' "${resolved}"
}
sha256_file() { sha256sum -- "$1" | awk '{print $1}'; }
state_value() { awk -F= -v key="$1" '$1==key {sub(/^[^=]*=/, ""); print; found=1} END {if(!found) exit 1}' "${STATE_FILE}"; }

proc_tail() { local line; [[ "$1" =~ ^[0-9]+$ && -r "/proc/$1/stat" ]] || return 1; IFS= read -r line <"/proc/$1/stat" || return 1; [[ "${line}" == *") "* ]] || return 1; printf '%s\n' "${line##*) }"; }
proc_field() { local tail; tail="$(proc_tail "$1")" || return 1; awk -v n="$2" '{print $n}' <<<"${tail}"; }
proc_state() { proc_field "$1" 1; }; proc_ppid() { proc_field "$1" 2; }; proc_pgid() { proc_field "$1" 3; }
proc_sid() { proc_field "$1" 4; }; proc_ticks() { proc_field "$1" 20; }
proc_uid() { awk '/^Uid:/ {print $2; exit}' "/proc/$1/status" 2>/dev/null; }
proc_env_value() { tr '\0' '\n' <"/proc/$1/environ" 2>/dev/null | sed -n "s/^$2=//p" | head -n1; }
proc_args() { local -n out="$2"; out=(); while IFS= read -r -d '' arg; do out+=("${arg}"); done <"/proc/$1/cmdline"; }
arg_present() { local wanted="$1"; shift; local arg; for arg in "$@"; do [[ "${arg}" == "${wanted}" ]] && return 0; done; return 1; }
arg_pair() { local key="$1" value="$2"; shift 2; local -a args=("$@"); local i; for ((i=0;i+1<${#args[@]};i++)); do [[ "${args[i]}" == "${key}" && "${args[i+1]}" == "${value}" ]] && return 0; done; return 1; }
controller_launcher_matches() { local pid="$1"; shift; local arg candidate cwd; cwd="$(readlink -e -- "/proc/${pid}/cwd")" || return 1; for arg in "$@"; do [[ "$(basename -- "${arg}")" == "$(basename -- "${LAUNCHER}")" ]] || continue; if [[ "${arg}" = /* ]]; then candidate="$(readlink -e -- "${arg}" 2>/dev/null)"; else candidate="$(readlink -e -- "${cwd}/${arg}" 2>/dev/null)"; fi; [[ "${candidate}" == "${LAUNCHER}" ]] && return 0; done; return 1; }
is_descendant() { local pid="$1" root="$2" parent steps=0; while [[ "${pid}" =~ ^[0-9]+$ && "${pid}" -gt 1 && "${steps}" -lt 128 ]]; do [[ "${pid}" == "${root}" ]] && return 0; parent="$(proc_ppid "${pid}" 2>/dev/null || true)"; [[ "${parent}" =~ ^[0-9]+$ && "${parent}" != "${pid}" ]] || return 1; pid="${parent}"; ((steps+=1)); done; return 1; }
identity_gone() { [[ ! -r "/proc/$1/stat" || "$(proc_ticks "$1" 2>/dev/null)" != "$2" || "$(proc_state "$1" 2>/dev/null)" == Z ]]; }
captured_start_gone() { local pid="$1" ticks="$2"; [[ -n "${pid}" ]] || return 0; if [[ -n "${ticks}" ]]; then identity_gone "${pid}" "${ticks}"; else [[ ! -r "/proc/${pid}/stat" ]] && ! kill -0 "${pid}" 2>/dev/null; fi; }

ss_rows() { local output; output="$(ss "$@" 2>/dev/null)" || return 1; printf '%s' "${output}"; }
port_closed() { local rows; rows="$(ss_rows -H -ltn "sport = :$1")" || return 1; [[ -z "${rows}" ]]; }
listener_pid() { local rows pids; rows="$(ss_rows -H -ltnp "sport = :$1")" || return 1; [[ -n "${rows}" ]] || return 1; grep -Eq "127\\.0\\.0\\.1:$1[[:space:]]" <<<"${rows}" || return 1; pids="$(grep -oE 'pid=[0-9]+' <<<"${rows}" | cut -d= -f2 | sort -u)"; [[ "$(wc -w <<<"${pids}")" -eq 1 ]] || return 1; printf '%s\n' "${pids}"; }

declare -A PARSED=() EXPORTED=()
CFG_PATH="" CFG_SHA="" CFG_WORKERS="" CFG_GPU_CSV="" CFG_HOST="" CFG_PORT="" CFG_STEP=""
CFG_MODEL="" CFG_SERVED="" CFG_REVISION="" CFG_RUNS="" CFG_BIN="" CFG_MAXLEN="" CFG_UTIL="" CFG_LD="" CFG_TP=""
declare -a CFG_GPUS=() CFG_PORTS=() CFG_UUIDS=()

parse_config() {
  local requested="$1" resolve="${2:-true}" path line number=0 name value was_export gpu rows uuid index
  path="$(canonical_regular "${requested}")" || die "config must be a regular non-symlink file: ${requested}"
  PARSED=(); EXPORTED=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    ((number+=1)); line="${line%$'\r'}"
    [[ "${line}" =~ ^[[:space:]]*$ || "${line}" =~ ^[[:space:]]*# ]] && continue
    if [[ "${line}" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Z_][A-Z0-9_]*)=\"([^\"\$\`\\]*)\"[[:space:]]*$ ]]; then
      name="${BASH_REMATCH[2]}"; value="${BASH_REMATCH[3]}"; was_export="${BASH_REMATCH[1]:-}"
    else
      die "config line ${number} is not a simple literal NAME=\"value\" assignment"
    fi
    [[ -z "${PARSED[${name}]+x}" ]] || die "duplicate config assignment: ${name}"
    case "${name}" in LOCAL_VLLM_API_KEY|VLLM_API_KEY|HF_TOKEN|HUGGING_FACE_HUB_TOKEN|OPENROUTER_API_KEY|GEMINI_API_KEY) die "credential assignment found in config" ;; esac
    case "${name}" in
      LOCOMO_DATA_DIR|CAPTURE_INTERFACE|VLLM_HOST|VLLM_PORT|VLLM_SECONDARY_PORT|VLLM_MODEL|VLLM_SERVED_MODEL_NAME|VLLM_MODEL_REVISION|CUDA_VISIBLE_DEVICES|PARALLEL_WORKERS|VLLM_PORT_STEP|TENSOR_PARALLEL_SIZE|MAX_MODEL_LEN|GPU_MEMORY_UTILIZATION|VLLM_BIN|LD_LIBRARY_PATH|RUNNER_PYTHON|COMPRESSOR_MODEL|COMPRESSOR_DEVICE|MANIFEST_PATH|SUMMARY_MANIFEST_PATH|MANIFEST_SHA256|SUMMARY_MANIFEST_SHA256|RUNS_ROOT|RANDOM_SEED|MAX_OUTPUT_TOKENS|SUMMARY_MAX_OUTPUT_TOKENS|REQUEST_TIMEOUT_SECONDS|MAIN_REPETITIONS|PROFILE|OBSERVATION_SECONDS|SUMMARY_OBSERVATION_SECONDS|CAPTURE_STOP_ON_RESPONSE|CAPTURE_STARTUP_DELAY_SECONDS|CLIENT_NETNS|HOST_VETH|CLIENT_VETH|HOST_VETH_CIDR|CLIENT_VETH_CIDR|SECURE_PROXY_HOST|CAPTURE_INTERFACE_OVERRIDE|CADDY_RUN_DIR|NETWORK_MTU|NETWORK_RTT_MS|NETWORK_UPLINK_MBIT|NETWORK_DOWNLINK_MBIT|NETWORK_QUEUE_PACKETS|LAB_NETWORKS|LINK_CALIBRATION_SECONDS|LAB_QA_SAMPLES|LAB_SUMMARY_SAMPLES|LAB_REPETITIONS|LAB_TRANSPORTS|LAB_WORKLOADS|CONNECTION_MODE|SESSION_TURNS|SESSION_START_INTERVAL_SECONDS|SESSION_BUDGET_SECONDS|SESSION_SEGMENT_SECONDS|SESSION_CONDITION|OPENROUTER_MODEL|OPENROUTER_PROVIDER|OPENROUTER_BASE_URL|GEMINI_MODEL|GEMINI_BASE_URL) ;;
      *) die "unknown config assignment: ${name}" ;;
    esac
    [[ -z "${was_export}" || "${name}" == LD_LIBRARY_PATH ]] || die "only LD_LIBRARY_PATH may use export"
    PARSED["${name}"]="${value}"
    [[ -z "${was_export}" ]] || EXPORTED["${name}"]=true
  done <"${path}"
  for name in PARALLEL_WORKERS CUDA_VISIBLE_DEVICES VLLM_HOST VLLM_PORT VLLM_SECONDARY_PORT VLLM_PORT_STEP VLLM_MODEL VLLM_SERVED_MODEL_NAME VLLM_MODEL_REVISION RUNS_ROOT VLLM_BIN MAX_MODEL_LEN GPU_MEMORY_UTILIZATION TENSOR_PARALLEL_SIZE LD_LIBRARY_PATH; do [[ -n "${PARSED[${name}]+x}" ]] || die "required config assignment missing: ${name}"; done
  [[ "${EXPORTED[LD_LIBRARY_PATH]:-}" == true ]] || die "LD_LIBRARY_PATH must use an explicit export assignment"
  CFG_PATH="${path}"; CFG_SHA="$(sha256_file "${path}")"; CFG_WORKERS="${PARSED[PARALLEL_WORKERS]}"; CFG_GPU_CSV="${PARSED[CUDA_VISIBLE_DEVICES]}"
  CFG_HOST="${PARSED[VLLM_HOST]}"; CFG_PORT="${PARSED[VLLM_PORT]}"; CFG_STEP="${PARSED[VLLM_PORT_STEP]}"; CFG_MODEL="${PARSED[VLLM_MODEL]}"; CFG_SERVED="${PARSED[VLLM_SERVED_MODEL_NAME]}"
  CFG_REVISION="${PARSED[VLLM_MODEL_REVISION]}"; CFG_RUNS="${PARSED[RUNS_ROOT]}"; CFG_BIN="${PARSED[VLLM_BIN]}"; CFG_MAXLEN="${PARSED[MAX_MODEL_LEN]}"; CFG_UTIL="${PARSED[GPU_MEMORY_UTILIZATION]}"; CFG_LD="${PARSED[LD_LIBRARY_PATH]:-}"; CFG_TP="${PARSED[TENSOR_PARALLEL_SIZE]}"
  [[ "${CFG_WORKERS}" == 1 || "${CFG_WORKERS}" == 2 ]] || die "PARALLEL_WORKERS must be 1 or 2"
  [[ "${CFG_HOST}" == 127.0.0.1 && "${CFG_PORT}" == 8000 && "${CFG_STEP}" == 1 ]] || die "vLLM must use loopback ports 8000/8001"
  [[ "${PARSED[VLLM_SECONDARY_PORT]}" == 8001 ]] || die "VLLM_SECONDARY_PORT must be 8001"
  [[ "${CFG_TP}" == 1 && "${CFG_MAXLEN}" =~ ^[1-9][0-9]*$ && "${CFG_UTIL}" =~ ^0\.[0-9]+$ ]] || die "invalid launch limits"
  [[ "${CFG_BIN}" = /* && -x "${CFG_BIN}" ]] || { [[ "${ACTION}" == validate-config ]] || die "VLLM_BIN must be an executable absolute path"; }
  IFS=, read -r -a CFG_GPUS <<<"${CFG_GPU_CSV}"; [[ "${#CFG_GPUS[@]}" -eq "${CFG_WORKERS}" && "${CFG_GPU_CSV}" != *[[:space:]]* ]] || die "CUDA_VISIBLE_DEVICES must list exactly one whitespace-free GPU per worker"
  [[ "${CFG_GPUS[0]}" =~ ^[0-9]+$ ]] || die "invalid GPU index"; [[ "${CFG_WORKERS}" == 1 || ( "${CFG_GPUS[1]}" =~ ^[0-9]+$ && "${CFG_GPUS[0]}" != "${CFG_GPUS[1]}" ) ]] || die "dual mode requires distinct GPU indices"
  CFG_PORTS=(); for ((index=0;index<CFG_WORKERS;index++)); do CFG_PORTS+=("$((CFG_PORT+index*CFG_STEP))"); done
  CFG_UUIDS=(); if [[ "${resolve}" == true ]]; then rows="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits)" || die "cannot query GPUs"; for gpu in "${CFG_GPUS[@]}"; do uuid="$(awk -F, -v g="${gpu}" '{gsub(/ /,"",$1); gsub(/^ +| +$/,"",$2); if($1==g)print $2}' <<<"${rows}")"; [[ -n "${uuid}" && "$(wc -w <<<"${uuid}")" -eq 1 ]] || die "GPU ${gpu} has no unique UUID"; CFG_UUIDS+=("${uuid}"); done; fi
}

save_cfg() { local p="$1" v src; for v in PATH SHA WORKERS GPU_CSV HOST PORT STEP MODEL SERVED REVISION RUNS BIN MAXLEN UTIL LD TP; do src="CFG_${v}"; printf -v "${p}_${v}" '%s' "${!src-}"; done; eval "${p}_GPUS=(\"\${CFG_GPUS[@]}\")"; eval "${p}_PORTS=(\"\${CFG_PORTS[@]}\")"; eval "${p}_UUIDS=(\"\${CFG_UUIDS[@]}\")"; }
load_cfg() { local p="$1" v src; for v in PATH SHA WORKERS GPU_CSV HOST PORT STEP MODEL SERVED REVISION RUNS BIN MAXLEN UTIL LD TP; do src="${p}_${v}"; printf -v "CFG_${v}" '%s' "${!src}"; done; eval "CFG_GPUS=(\"\${${p}_GPUS[@]}\")"; eval "CFG_PORTS=(\"\${${p}_PORTS[@]}\")"; eval "CFG_UUIDS=(\"\${${p}_UUIDS[@]}\")"; }

freeze_config() { local source="$1" hash="$2" label="$3" tmp; tmp="$(mktemp "${STATE_DIR}/.${label}.XXXXXX.env")" || return 1; cp -- "${source}" "${tmp}" || { rm -f -- "${tmp}"; return 1; }; chmod 400 "${tmp}" || { rm -f -- "${tmp}"; return 1; }; [[ "$(sha256_file "${source}")" == "${hash}" && "$(sha256_file "${tmp}")" == "${hash}" ]] || { rm -f -- "${tmp}"; return 1; }; sync -f "${tmp}" || { rm -f -- "${tmp}"; return 1; }; printf '%s\n' "${tmp}"; }

remove_unused_frozen_config() {
  local path="$1"
  [[ -n "${path}" && "${path}" != "${KEEP_FROZEN}" ]] || return 0
  [[ ! -e "${path}" && ! -L "${path}" ]] && return 0
  case "${path}" in
    "${STATE_DIR}"/.previous.*.env|"${STATE_DIR}"/.target.*.env) ;;
    *) printf 'Preserving unexpected frozen-config path: %s\n' "${path}" >&2; return 1 ;;
  esac
  [[ -f "${path}" && ! -L "${path}" &&
    "$(stat -c %u -- "${path}")" == "$(id -u)" &&
    "$(stat -c %h -- "${path}")" == 1 ]] || {
    printf 'Preserving unsafe frozen-config path: %s\n' "${path}" >&2
    return 1
  }
  rm -- "${path}"
}

cleanup_switcher_exit() {
  local status=$?
  trap - EXIT
  remove_unused_frozen_config "${PREV_FROZEN}" || true
  remove_unused_frozen_config "${TARGET_FROZEN}" || true
  [[ -z "${LOCK_DIR}" ]] || rmdir -- "${LOCK_DIR}" 2>/dev/null || true
  exit "${status}"
}

validate_controller() { local pid="$1" ticks="$2" config="$3" uid="$4" args=(); [[ "$(proc_ticks "${pid}" 2>/dev/null)" == "${ticks}" && "$(proc_state "${pid}" 2>/dev/null)" != Z && "$(proc_uid "${pid}")" == "${uid}" && "${uid}" == "$(id -u)" ]] || return 1; proc_args "${pid}" args || return 1; controller_launcher_matches "${pid}" "${args[@]}" || return 1; [[ "$(readlink -e -- "$(proc_env_value "${pid}" EXPERIMENT_ENV_FILE)" 2>/dev/null)" == "${config}" ]]; }
verify_api_argv_env() { local pid="$1" worker="$2" args=(); proc_args "${pid}" args || return 1; arg_present "${CFG_BIN}" "${args[@]}" || return 1; arg_present serve "${args[@]}" || return 1; arg_present "${CFG_MODEL}" "${args[@]}" || return 1; arg_pair --host "${CFG_HOST}" "${args[@]}" && arg_pair --port "${CFG_PORTS[worker]}" "${args[@]}" && arg_pair --served-model-name "${CFG_SERVED}" "${args[@]}" && arg_pair --tensor-parallel-size "${CFG_TP}" "${args[@]}" && arg_pair --max-model-len "${CFG_MAXLEN}" "${args[@]}" && arg_pair --gpu-memory-utilization "${CFG_UTIL}" "${args[@]}" && arg_present --language-model-only "${args[@]}" || return 1; [[ -z "${CFG_REVISION}" ]] || arg_pair --revision "${CFG_REVISION}" "${args[@]}" || return 1; [[ "$(proc_env_value "${pid}" CUDA_VISIBLE_DEVICES)" == "${CFG_GPUS[worker]}" && "$(proc_env_value "${pid}" LD_LIBRARY_PATH)" == "${CFG_LD}" ]]; }
gpu_engine_pid() { local uuid="$1" root="$2" rows u p found=""; rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null)" || return 1; while IFS=, read -r u p; do u="${u//[[:space:]]/}"; p="${p//[[:space:]]/}"; [[ "${u}" == "${uuid}" && "${p}" =~ ^[0-9]+$ ]] || continue; is_descendant "${p}" "${root}" || continue; [[ -z "${found}" ]] || return 1; found="${p}"; done <<<"${rows}"; [[ -n "${found}" ]] || return 1; printf '%s\n' "${found}"; }

VC="" VT="" VKEY=""; declare -a VAP=() VAT=() VEP=() VET=()
verify_structure() { local controller="$1" ticks="$2" config="$3" uid="$4" i api engine; validate_controller "${controller}" "${ticks}" "${config}" "${uid}" || return 1; VAP=();VAT=();VEP=();VET=(); for ((i=0;i<CFG_WORKERS;i++)); do api="$(listener_pid "${CFG_PORTS[i]}")" || return 1; [[ "$(proc_uid "${api}")" == "${uid}" ]] && is_descendant "${api}" "${controller}" && [[ "$(proc_pgid "${api}")" == "${api}" && "$(proc_sid "${api}")" == "${api}" ]] || return 1; verify_api_argv_env "${api}" "${i}" || return 1; engine="$(gpu_engine_pid "${CFG_UUIDS[i]}" "${controller}")" || return 1; [[ "$(proc_uid "${engine}")" == "${uid}" ]] || return 1; VAP+=("${api}"); VAT+=("$(proc_ticks "${api}")"); VEP+=("${engine}"); VET+=("$(proc_ticks "${engine}")"); done; [[ "${CFG_WORKERS}" == 2 ]] || port_closed 8001 || return 1; VC="${controller}"; VT="${ticks}"; }
health_all() { local key="$1" port; [[ -n "${key}" && "${key}" != *$'\n'* && "${key}" != *$'\r'* ]] || return 1; for port in "${CFG_PORTS[@]}"; do printf 'Authorization: Bearer %s\n' "${key}" | curl -fsS --max-time 10 --header @- -o /dev/null "http://127.0.0.1:${port}/v1/models" || return 1; done; }

verify_state() { local schema config hash controller ticks uid i; [[ "$(stat -c %u -- "${STATE_FILE}")" == "$(id -u)" ]] || return 1; schema="$(state_value schema)" || return 1; [[ "${schema}" == commu-vllm-service-state-v1 || "${schema}" == commu-vllm-service-state-v2 ]] || return 1; [[ "$(state_value status)" == running ]] || return 1; config="$(canonical_regular "$(state_value config)")" || return 1; hash="$(state_value config_sha256)" || return 1; [[ "$(sha256_file "${config}")" == "${hash}" ]] || return 1; parse_config "${config}"; save_cfg PREV; controller="$(state_value controller_pid)" || return 1; ticks="$(state_value controller_start_ticks)" || return 1; uid="$(state_value controller_uid 2>/dev/null || id -u)"; verify_structure "${controller}" "${ticks}" "${config}" "${uid}" || return 1; [[ "${schema}" == commu-vllm-service-state-v1 ]] || { [[ "$(state_value worker_count)" == "${CFG_WORKERS}" && "$(state_value controller_pgid)" == "$(proc_pgid "${controller}")" && "$(state_value controller_sid)" == "$(proc_sid "${controller}")" ]] || return 1; }; for ((i=0;i<CFG_WORKERS;i++)); do [[ "$(state_value worker_${i}_port)" == "${CFG_PORTS[i]}" && "$(state_value worker_${i}_gpu_index)" == "${CFG_GPUS[i]}" && "$(state_value worker_${i}_gpu_uuid)" == "${CFG_UUIDS[i]}" && "$(state_value worker_${i}_api_pid)" == "${VAP[i]}" && "$(state_value worker_${i}_engine_pid)" == "${VEP[i]}" ]] || return 1; if [[ "${schema}" == commu-vllm-service-state-v2 ]]; then [[ "$(state_value worker_${i}_api_start_ticks)" == "${VAT[i]}" && "$(state_value worker_${i}_engine_start_ticks)" == "${VET[i]}" ]] || return 1; fi; done; PREV_SCHEMA="${schema}"; PREV_UID="${uid}"; }

audit_target_gpus() { local rows u p allowed engine; rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)" || return 1; while IFS=, read -r u p; do u="${u//[[:space:]]/}"; p="${p//[[:space:]]/}"; [[ "${p}" =~ ^[0-9]+$ ]] || continue; allowed=false; for target_uuid in "${TARGET_UUIDS[@]}"; do [[ "${u}" == "${target_uuid}" ]] || continue; for engine in "${VEP[@]}"; do [[ "${p}" == "${engine}" ]] && allowed=true; done; [[ "${allowed}" == true ]] || return 1; done; done <<<"${rows}"; }
configured_gpus_empty() { local rows u p uuid; rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)" || return 1; while IFS=, read -r u p; do u="${u//[[:space:]]/}"; p="${p//[[:space:]]/}"; [[ "${p}" =~ ^[0-9]+$ ]] || continue; for uuid in "${CFG_UUIDS[@]}"; do [[ "${u}" != "${uuid}" ]] || return 1; done; done <<<"${rows}"; }
capture_owned_resources() { local controller="$1" rows u p uuid expected api port; VAP=();VAT=();VEP=();VET=(); for port in "${CFG_PORTS[@]}"; do if port_closed "${port}"; then continue; fi; api="$(listener_pid "${port}")" || return 1; [[ "$(proc_uid "${api}")" == "$(id -u)" ]] && is_descendant "${api}" "${controller}" || return 1; VAP+=("${api}"); VAT+=("$(proc_ticks "${api}")"); done; rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)" || return 1; while IFS=, read -r u p; do u="${u//[[:space:]]/}"; p="${p//[[:space:]]/}"; [[ "${p}" =~ ^[0-9]+$ ]] || continue; is_descendant "${p}" "${controller}" || continue; expected=false; for uuid in "${CFG_UUIDS[@]}"; do [[ "${u}" == "${uuid}" ]] && expected=true; done; [[ "${expected}" == true ]] || return 1; VEP+=("${p}"); VET+=("$(proc_ticks "${p}")"); done <<<"${rows}"; }
old_resources_gone() { local i rows u p oldp; for ((i=0;i<${#VAP[@]};i++)); do identity_gone "${VAP[i]}" "${VAT[i]}" || return 1; done; for ((i=0;i<${#VEP[@]};i++)); do identity_gone "${VEP[i]}" "${VET[i]}" || return 1; done; for port in 8000 8001; do port_closed "${port}" || return 1; done; rows="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)" || return 1; while IFS=, read -r u p; do p="${p//[[:space:]]/}"; for oldp in "${VEP[@]}"; do [[ "${p}" != "${oldp}" ]] || return 1; done; done <<<"${rows}"; }
stop_controller() { local pid="$1" ticks="$2" config="$3" uid="$4" attempt; validate_controller "${pid}" "${ticks}" "${config}" "${uid}" || return 1; capture_owned_resources "${pid}" || return 1; kill -TERM "${pid}" || return 1; for ((attempt=0;attempt<TIMEOUT*10;attempt++)); do if identity_gone "${pid}" "${ticks}" && old_resources_gone; then return 0; fi; sleep .1; done; return 1; }

START_PID="" START_TICKS=""
start_service() { local config="$1" hash="$2" key="$3" log="$4" attempt; [[ "$(sha256_file "${config}")" == "${hash}" ]] || return 1; (export EXPERIMENT_ENV_FILE="${config}" LOCAL_VLLM_API_KEY="${key}"; exec nohup setsid bash "${LAUNCHER}") >>"${log}" 2>&1 & START_PID=$!; for ((attempt=0;attempt<100;attempt++)); do START_TICKS="$(proc_ticks "${START_PID}" 2>/dev/null || true)"; [[ -n "${START_TICKS}" ]] && return 0; kill -0 "${START_PID}" 2>/dev/null || return 1; sleep .01; done; return 1; }
wait_ready() { local config="$1" key="$2" attempt; for ((attempt=0;attempt<TIMEOUT;attempt++)); do verify_structure "${START_PID}" "${START_TICKS}" "${config}" "$(id -u)" && health_all "${key}" && return 0; sleep 1; done; return 1; }

write_state() { local config="$1" result="$2" tmp i repo; tmp="$(mktemp "${STATE_DIR}/.$(basename "${STATE_FILE}").XXXXXX")" || return 1; trap 'rm -f -- "${tmp:-}"' RETURN; chmod 600 "${tmp}" || return 1; repo="$(git -C "${EXPERIMENT_ROOT}" rev-parse HEAD 2>/dev/null || printf unknown)"; { printf 'schema=commu-vllm-service-state-v2\nstatus=running\nrepository_sha=%s\nconfig=%s\nconfig_sha256=%s\ncontroller_pid=%s\ncontroller_start_ticks=%s\ncontroller_uid=%s\ncontroller_pgid=%s\ncontroller_sid=%s\nworker_count=%s\nswitch_result=%s\n' "${repo}" "${config}" "$(sha256_file "${config}")" "${VC}" "${VT}" "$(id -u)" "$(proc_pgid "${VC}")" "$(proc_sid "${VC}")" "${CFG_WORKERS}" "${result}"; for ((i=0;i<CFG_WORKERS;i++)); do printf 'worker_%s_port=%s\nworker_%s_gpu_index=%s\nworker_%s_gpu_uuid=%s\nworker_%s_api_pid=%s\nworker_%s_api_start_ticks=%s\nworker_%s_engine_pid=%s\nworker_%s_engine_start_ticks=%s\n' "$i" "${CFG_PORTS[i]}" "$i" "${CFG_GPUS[i]}" "$i" "${CFG_UUIDS[i]}" "$i" "${VAP[i]}" "$i" "${VAT[i]}" "$i" "${VEP[i]}" "$i" "${VET[i]}"; done; printf 'started_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } >"${tmp}" || return 1; sync -f "${tmp}" || return 1; mv -- "${tmp}" "${STATE_FILE}" || return 1; tmp=""; sync -f "${STATE_DIR}" || return 1; trap - RETURN; }
prepare_new_log() { local parent; parent="$(dirname -- "$1")"; [[ "$1" = /* && -d "${parent}" && ! -L "${parent}" && "$(stat -c %u -- "${parent}")" == "$(id -u)" && ! -e "$1" && ! -L "$1" ]] || return 1; (set -o noclobber; : >"$1") 2>/dev/null || return 1; [[ -f "$1" && ! -L "$1" && "$(stat -c %u -- "$1")" == "$(id -u)" && "$(stat -c %h -- "$1")" == 1 ]] || return 1; chmod 600 "$1"; }

if [[ "${ACTION}" == validate-config ]]; then [[ -n "${TARGET_ENV}" ]] || die "--target-env required"; parse_config "${TARGET_ENV}" false; printf 'VLLM_TOPOLOGY_CONFIG_OK workers=%s gpu_indices=%s ports=' "${CFG_WORKERS}" "${CFG_GPU_CSV}"; (IFS=,; printf '%s\n' "${CFG_PORTS[*]}"); exit 0; fi
[[ -n "${STATE_FILE}" && "${STATE_FILE}" = /* ]] || die "absolute --state required"; STATE_DIR="$(dirname -- "${STATE_FILE}")"; [[ -d "${STATE_DIR}" && ! -L "${STATE_DIR}" && "$(stat -c %u -- "${STATE_DIR}")" == "$(id -u)" ]] || die "state directory must be real and user-owned"; STATE_FILE="$(canonical_regular "${STATE_FILE}")" || die "state must be an existing regular non-symlink file"; [[ "$(stat -c %u -- "${STATE_FILE}")" == "$(id -u)" && "$(stat -c %h -- "${STATE_FILE}")" == 1 ]] || die "state must be singly-linked and user-owned"
for c in awk curl git mktemp nvidia-smi nohup readlink setsid sha256sum ss stat sync; do command -v "${c}" >/dev/null || die "missing command: ${c}"; done
LOCK_DIR="${STATE_FILE}.lock.d"; mkdir -- "${LOCK_DIR}" 2>/dev/null || die "another operation holds the state lock"; [[ ! -L "${LOCK_DIR}" && "$(stat -c %F -- "${LOCK_DIR}")" == directory && "$(stat -c %u -- "${LOCK_DIR}")" == "$(id -u)" ]] || die "unsafe lock"; trap cleanup_switcher_exit EXIT
verify_state || die "state/live identity verification failed; nothing signalled"
if [[ "${ACTION}" == check ]]; then VKEY="$(proc_env_value "${VC}" LOCAL_VLLM_API_KEY)"; health_all "${VKEY}" || die "authenticated health failed"; printf 'VLLM_TOPOLOGY_OK workers=%s gpu_indices=%s\n' "${CFG_WORKERS}" "${CFG_GPU_CSV}"; exit 0; fi
[[ -n "${TARGET_ENV}" ]] || die "--target-env required"; parse_config "${TARGET_ENV}"; save_cfg TARGET
# Both inputs are now inertly parsed. Freeze them before the credential is read.
PREV_FROZEN="$(freeze_config "${PREV_PATH}" "${PREV_SHA}" previous)" || die "could not freeze previous config"; TARGET_FROZEN="$(freeze_config "${TARGET_PATH}" "${TARGET_SHA}" target)" || die "could not freeze target config"
load_cfg PREV; PREV_ORIGINAL="${CFG_PATH}"; PREV_ORIGINAL_SHA="${CFG_SHA}"; CFG_PATH="${PREV_FROZEN}"; CFG_SHA="$(sha256_file "${PREV_FROZEN}")"; save_cfg PREV
load_cfg TARGET; CFG_PATH="${TARGET_FROZEN}"; CFG_SHA="$(sha256_file "${TARGET_FROZEN}")"; save_cfg TARGET
load_cfg PREV; audit_target_gpus || die "target GPU has an unrelated compute process"
VKEY="$(proc_env_value "${VC}" LOCAL_VLLM_API_KEY)"; health_all "${VKEY}" || die "credential recovery/authenticated health failed"
audit_target_gpus || die "target GPU occupancy changed before cutover"
OLD_C="${VC}"; OLD_T="${VT}"; OLD_CONFIG="${PREV_ORIGINAL}"; OLD_UID="${PREV_UID}"; OLD_AP=("${VAP[@]}"); OLD_AT=("${VAT[@]}"); OLD_EP=("${VEP[@]}"); OLD_ET=("${VET[@]}")
if [[ -z "${LOG_FILE}" ]]; then LOG_FILE="${STATE_DIR}/vllm-switch-$(date -u +%Y%m%dT%H%M%SZ).log"; fi; prepare_new_log "${LOG_FILE}" || die "log must be a new user-owned regular file"
stop_controller "${OLD_C}" "${OLD_T}" "${OLD_CONFIG}" "${OLD_UID}" || die "old service did not stop completely; state preserved"
load_cfg TARGET; target_ok=false; target_launched=false
if configured_gpus_empty; then
  KEEP_FROZEN="${TARGET_FROZEN}"
  if start_service "${TARGET_PATH}" "${TARGET_SHA}" "${VKEY}" "${LOG_FILE}"; then
    target_launched=true
    wait_ready "${TARGET_PATH}" "${VKEY}" && target_ok=true
  elif [[ -n "${START_PID}" ]]; then
    target_launched=true
  fi
else
  printf 'Target GPU occupancy changed after old-service shutdown; target start refused.\n' >&2
fi
if [[ "${target_ok}" == true ]] && write_state "${TARGET_PATH}" switched; then unset VKEY; printf 'VLLM_TOPOLOGY_SWITCH_OK workers=%s gpu_indices=%s\n' "${CFG_WORKERS}" "${CFG_GPU_CSV}"; exit 0; fi
printf 'Target startup or state publication failed; rolling back. Diagnostics: %s\n' "${LOG_FILE}" >&2
if [[ "${target_launched}" == true ]]; then
  if [[ -n "${START_PID}" && -n "${START_TICKS}" ]] && validate_controller "${START_PID}" "${START_TICKS}" "${TARGET_PATH}" "$(id -u)"; then
    stop_controller "${START_PID}" "${START_TICKS}" "${TARGET_PATH}" "$(id -u)" || die "verified target could not be fully stopped"
    KEEP_FROZEN=""
  else
    captured_start_gone "${START_PID}" "${START_TICKS}" || die "captured target controller identity is still live but unverifiable; rollback refused"
    for p in 8000 8001; do port_closed "${p}" || die "unowned listener remains; rollback refused"; done
    configured_gpus_empty || die "target GPU process remains; rollback refused"
    KEEP_FROZEN=""
  fi
fi
load_cfg PREV; configured_gpus_empty || die "previous topology GPUs are no longer free; rollback start refused"
RLOG="${LOG_FILE}.rollback"; prepare_new_log "${RLOG}" || die "could not create rollback log"
KEEP_FROZEN="${PREV_FROZEN}"
if start_service "${PREV_PATH}" "${PREV_SHA}" "${VKEY}" "${RLOG}" && wait_ready "${PREV_PATH}" "${VKEY}"; then
  if write_state "${PREV_PATH}" rolled_back; then unset VKEY; printf 'ERROR: target failed; previous topology restored\n' >&2; exit 1; fi
  # Publication is part of the transaction: never leave an unrecorded service.
  stop_controller "${START_PID}" "${START_TICKS}" "${PREV_PATH}" "$(id -u)" || die "rollback state publication failed and rollback could not be stopped"
  KEEP_FROZEN=""
fi
if [[ -n "${START_PID}" && -n "${START_TICKS}" ]] && validate_controller "${START_PID}" "${START_TICKS}" "${PREV_PATH}" "$(id -u)"; then
  stop_controller "${START_PID}" "${START_TICKS}" "${PREV_PATH}" "$(id -u)" || die "failed rollback could not be stopped"
  KEEP_FROZEN=""
else
  captured_start_gone "${START_PID}" "${START_TICKS}" || die "captured rollback controller identity is still live but unverifiable"
  for p in 8000 8001; do port_closed "${p}" || die "failed rollback left an unowned listener"; done
  configured_gpus_empty || die "failed rollback left a GPU process"
  KEEP_FROZEN=""
fi
unset VKEY; die "target and rollback failed; no healthy unrecorded service was intentionally left running"
