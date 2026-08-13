#!/usr/bin/env bash

# Shared measured-run topology discovery. Source after scripts/lib.sh.

load_measured_worker_topology() {
  if [[ ! "${PARALLEL_WORKERS:-}" =~ ^[12]$ ]]; then
    echo "PARALLEL_WORKERS must be 1 or 2." >&2
    return 2
  fi
  if [[ ! "${VLLM_PORT:-}" =~ ^[0-9]+$ ||
    ! "${VLLM_PORT_STEP:-}" =~ ^[0-9]+$ ]]; then
    echo "VLLM_PORT and VLLM_PORT_STEP must be non-negative integers." >&2
    return 2
  fi
  if [[ -z "${VLLM_MODEL:-}" || -z "${VLLM_SERVED_MODEL_NAME:-}" ]]; then
    echo "VLLM_MODEL and VLLM_SERVED_MODEL_NAME must be non-empty." >&2
    return 2
  fi
  require_command nvidia-smi

  local -a selectors inventory discovered_uuids=()
  local -a tls_ports=(8443 8543) http3_ports=(8444 8544)
  local worker selector line inventory_index inventory_uuid matched
  local topology_api=0
  if declare -F load_worker_topology >/dev/null; then
    topology_api=1
    load_worker_topology
    if [[ "${TOPOLOGY_WORKER_COUNT:-}" != "${PARALLEL_WORKERS}" ]]; then
      echo "Loaded topology worker count does not match PARALLEL_WORKERS." >&2
      return 2
    fi
    selectors=("${WORKER_GPU_IDS[@]}")
    if declare -F load_worker_gpu_identities >/dev/null; then
      load_worker_gpu_identities
      discovered_uuids=("${WORKER_GPU_UUIDS[@]}")
    fi
    if declare -F configure_proxy_ports >/dev/null; then
      configure_proxy_ports "${TOPOLOGY_WORKER_COUNT}"
      tls_ports=("${EXPECTED_PROXY_TCP_PORTS[@]}")
      http3_ports=("${EXPECTED_PROXY_UDP_PORTS[@]}")
    fi
  else
    IFS=',' read -r -a selectors <<<"${CUDA_VISIBLE_DEVICES:-0,1}"
    WORKER_VLLM_PORTS=()
    for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
      WORKER_VLLM_PORTS+=("$((VLLM_PORT + worker * VLLM_PORT_STEP))")
    done
  fi
  if ((${#selectors[@]} < PARALLEL_WORKERS)); then
    echo "CUDA_VISIBLE_DEVICES must list at least ${PARALLEL_WORKERS} GPU selectors." >&2
    return 2
  fi
  if [[ "${topology_api}" -eq 0 ]]; then
    mapfile -t inventory < <(
      nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits
    )
    if ((${#inventory[@]} == 0)); then
      echo "nvidia-smi returned no physical GPUs." >&2
      return 2
    fi
  fi

  WORKER_GPU_SELECTORS=()
  WORKER_GPU_INDEXES=()
  WORKER_GPU_UUIDS=()
  WORKER_TLS_PORTS=()
  WORKER_HTTP3_PORTS=()
  for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
    selector="${selectors[worker]}"
    selector="${selector#"${selector%%[![:space:]]*}"}"
    selector="${selector%"${selector##*[![:space:]]}"}"
    if [[ "${topology_api}" -eq 1 ]]; then
      if [[ ! "${selector}" =~ ^[0-9]+$ || -z "${discovered_uuids[worker]:-}" ]]; then
        echo "Loaded topology is missing a physical GPU index or UUID for worker ${worker}." >&2
        return 2
      fi
      WORKER_GPU_SELECTORS+=("${selector}")
      WORKER_GPU_INDEXES+=("${selector}")
      WORKER_GPU_UUIDS+=("${discovered_uuids[worker]}")
      WORKER_TLS_PORTS+=("${tls_ports[worker]}")
      WORKER_HTTP3_PORTS+=("${http3_ports[worker]}")
      continue
    fi
    matched=0
    for line in "${inventory[@]}"; do
      IFS=',' read -r inventory_index inventory_uuid <<<"${line}"
      inventory_index="${inventory_index//[[:space:]]/}"
      inventory_uuid="${inventory_uuid//[[:space:]]/}"
      if [[ "${selector}" == "${inventory_index}" ||
        "${selector}" == "${inventory_uuid}" ]]; then
        WORKER_GPU_SELECTORS+=("${selector}")
        WORKER_GPU_INDEXES+=("${inventory_index}")
        WORKER_GPU_UUIDS+=("${inventory_uuid}")
        matched=1
        break
      fi
    done
    if [[ "${matched}" -ne 1 ]]; then
      echo "CUDA_VISIBLE_DEVICES selector '${selector}' is not a physical GPU index or UUID." >&2
      return 2
    fi
    WORKER_TLS_PORTS+=("${tls_ports[worker]}")
    WORKER_HTTP3_PORTS+=("${http3_ports[worker]}")
  done
  if [[ "${PARALLEL_WORKERS}" -eq 2 &&
    ( "${WORKER_GPU_INDEXES[0]}" == "${WORKER_GPU_INDEXES[1]}" ||
      "${WORKER_GPU_UUIDS[0]}" == "${WORKER_GPU_UUIDS[1]}" ) ]]; then
    echo "Measured workers must use distinct physical GPUs." >&2
    return 2
  fi
}

ensure_worker_topology() {
  local output_root="$1" worker
  local -a arguments=(
    -P -m traffic_experiment.traffic_measure.worker_topology
    --output-root "${output_root}"
    --worker-count "${PARALLEL_WORKERS}"
    --model "${VLLM_MODEL}"
    --served-model-name "${VLLM_SERVED_MODEL_NAME}"
    --model-revision "${VLLM_MODEL_REVISION:-}"
  )
  for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
    arguments+=(
      --worker
      "${worker}|${WORKER_GPU_SELECTORS[worker]}|${WORKER_GPU_INDEXES[worker]}|${WORKER_GPU_UUIDS[worker]}|${WORKER_VLLM_PORTS[worker]}|${WORKER_TLS_PORTS[worker]}|${WORKER_HTTP3_PORTS[worker]}"
    )
  done
  "${RUNNER_PYTHON}" "${arguments[@]}"
}
