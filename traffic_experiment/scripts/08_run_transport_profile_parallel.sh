#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
source "${SCRIPT_DIR}/worker_topology.sh"

require_value CAPTURE_INTERFACE
require_value LOCAL_VLLM_API_KEY
require_command dumpcap
require_command tshark
require_command setsid

PARALLEL_WORKERS="${PARALLEL_WORKERS:-2}"
VLLM_PORT_STEP="${VLLM_PORT_STEP:-1}"
load_measured_worker_topology

TRANSPORT="${TRANSPORT:-tls13}"
SECURE_PROXY_HOST="${SECURE_PROXY_HOST:-localhost}"
CAPTURE_INTERFACE_EFFECTIVE="${CAPTURE_INTERFACE_OVERRIDE:-${CAPTURE_INTERFACE}}"
CONNECTION_MODE="${CONNECTION_MODE:-warm}"
RUN_PREFIX=()
if [[ -n "${CLIENT_NETNS:-}" ]]; then
  require_command ip
  CAPTURE_INTERFACE_EFFECTIVE="${CAPTURE_INTERFACE_OVERRIDE:-${CLIENT_VETH:-llmclient0}}"
  if ! ip netns exec "${CLIENT_NETNS}" \
    ip link show dev "${CAPTURE_INTERFACE_EFFECTIVE}" >/dev/null 2>&1; then
    echo "Capture interface '${CAPTURE_INTERFACE_EFFECTIVE}' is not present in namespace '${CLIENT_NETNS}'." >&2
    echo "Set CAPTURE_INTERFACE_OVERRIDE to that namespace's client veth (normally ${CLIENT_VETH:-llmclient0})." >&2
    exit 2
  fi
  RUN_PREFIX=(ip netns exec "${CLIENT_NETNS}")
fi
case "${TRANSPORT}" in
  tls13)
    PORTS=("${WORKER_TLS_PORTS[@]}")
    FILTER_PROTOCOL=tcp
    require_command curl
    ;;
  http3)
    PORTS=("${WORKER_HTTP3_PORTS[@]}")
    FILTER_PROTOCOL=udp
    if ! "${RUNNER_PYTHON}" -c 'import aioquic' >/dev/null 2>&1; then
      echo "aioquic is absent; rerun 01_setup_runner.sh." >&2
      exit 2
    fi
    ;;
  *)
    echo "TRANSPORT must be tls13 or http3." >&2
    exit 2
    ;;
esac

case "${PROFILE}" in
  pilot) SAMPLES=8; REPETITIONS=3 ;;
  main) SAMPLES=32; REPETITIONS="${MAIN_REPETITIONS:-3}" ;;
  robustness) SAMPLES=32; REPETITIONS=10 ;;
  *) echo "PROFILE must be pilot, main, or robustness." >&2; exit 2 ;;
esac
SAMPLES="${SAMPLES_OVERRIDE:-${SAMPLES}}"
REPETITIONS="${REPETITIONS_OVERRIDE:-${REPETITIONS}}"

CADDY_RUN_DIR="$(absolute_from_experiment "${CADDY_RUN_DIR:-runs/caddy}")"
CA_FILE="${CADDY_RUN_DIR}/data/caddy/pki/authorities/local/root.crt"
if [[ ! -f "${CA_FILE}" ]]; then
  echo "Missing ${CA_FILE}; run 07_start_secure_proxy.sh first." >&2
  exit 2
fi

MANIFEST_PATH_EFFECTIVE="${MANIFEST_PATH_OVERRIDE:-${MANIFEST_PATH}}"
RUNS_ROOT_EFFECTIVE="${RUNS_ROOT_OVERRIDE:-${RUNS_ROOT}}"
MAX_OUTPUT_TOKENS_EFFECTIVE="${MAX_OUTPUT_TOKENS_OVERRIDE:-${MAX_OUTPUT_TOKENS}}"
OBSERVATION_SECONDS_EFFECTIVE="${OBSERVATION_SECONDS_OVERRIDE:-${OBSERVATION_SECONDS}}"
CAPTURE_COMPLETION_ARGS=()
if [[ "${CAPTURE_STOP_ON_RESPONSE:-false}" == "true" ]]; then
  CAPTURE_COMPLETION_ARGS=(--capture-stop-on-response)
fi
MANIFEST_ABS="$(absolute_from_experiment "${MANIFEST_PATH_EFFECTIVE}")"
OUTPUT_ROOT="$(absolute_from_experiment "${RUNS_ROOT_EFFECTIVE}")"
ensure_worker_topology "${OUTPUT_ROOT}"
RUN_DIR="${OUTPUT_ROOT}/local_vllm_${TRANSPORT}_${PROFILE}"
RESULTS="${RUN_DIR}/results.jsonl"
if [[ -L "${RUN_DIR}" || -L "${RESULTS}" ]]; then
  echo "Refusing symlinked run directory or merged results path under ${RUN_DIR}." >&2
  exit 2
fi
mkdir -p "${RUN_DIR}"
LAUNCHER_STATE="${RUN_DIR}/launcher.state"
LAUNCHER_START_TICKS="$(process_start_ticks "$$")"
LAUNCHER_SCRIPT="$(readlink -f -- "${BASH_SOURCE[0]}")"

pids=()
pid_start_ticks=()
pid_active=()

register_child() {
  local pid="$1"
  local ticks
  if ! ticks="$(record_owned_session_start_ticks "${pid}")"; then
    echo "Could not record exact identity for measurement child PID ${pid}." >&2
    cleanup_failed_session_registration "${pid}" "measurement worker"
  fi
  pids+=("${pid}")
  pid_start_ticks+=("${ticks}")
  pid_active+=(1)
}

cleanup_children_on_exit() {
  local status=$?
  local cleanup_failed=0
  local index
  trap - EXIT
  for index in "${!pids[@]}"; do
    [[ "${pid_active[index]:-0}" -eq 1 ]] || continue
    if stop_owned_child \
      "${pids[index]}" "${pid_start_ticks[index]}" "measurement worker ${index}"; then
      pid_active[index]=0
    else
      cleanup_failed=1
    fi
  done
  if ! remove_owned_launcher_state; then
    cleanup_failed=1
  fi
  if [[ "${cleanup_failed}" -ne 0 && "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}

launcher_state_value() {
  local key="$1"
  awk -F= -v key="${key}" \
    '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${LAUNCHER_STATE}"
}

remove_owned_launcher_state() {
  [[ -e "${LAUNCHER_STATE}" || -L "${LAUNCHER_STATE}" ]] || return 0
  if [[ -L "${LAUNCHER_STATE}" ]]; then
    echo "Refusing to remove symlinked launcher state ${LAUNCHER_STATE}." >&2
    return 1
  fi
  if [[ "$(launcher_state_value owner)" != "commu-transport-launcher-v1" ||
    "$(launcher_state_value pid)" != "$$" ||
    "$(launcher_state_value start_ticks)" != "${LAUNCHER_START_TICKS}" ||
    "$(launcher_state_value script)" != "${LAUNCHER_SCRIPT}" ]]; then
    echo "Refusing to remove launcher state not owned by this invocation." >&2
    return 1
  fi
  rm -f "${LAUNCHER_STATE}"
}

write_launcher_state() {
  local temporary="${LAUNCHER_STATE}.tmp.$$"
  if [[ -e "${LAUNCHER_STATE}" || -L "${LAUNCHER_STATE}" ||
    -e "${temporary}" || -L "${temporary}" ]]; then
    echo "Refusing to overwrite launcher state ${LAUNCHER_STATE}." >&2
    return 1
  fi
  umask 077
  {
    printf 'owner=commu-transport-launcher-v1\n'
    printf 'pid=%s\n' "$$"
    printf 'start_ticks=%s\n' "${LAUNCHER_START_TICKS}"
    printf 'script=%s\n' "${LAUNCHER_SCRIPT}"
  } >"${temporary}"
  mv -T "${temporary}" "${LAUNCHER_STATE}"
}

trap cleanup_children_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
write_launcher_state

for ((worker=0; worker<PARALLEL_WORKERS; worker++)); do
  port="${PORTS[$worker]}"
  worker_dir="${RUN_DIR}/worker-${worker}"
  worker_log="${RUN_DIR}/worker-${worker}.log"
  if [[ -L "${worker_dir}" || -L "${worker_log}" ||
    -L "${worker_dir}/results.jsonl" || -L "${worker_dir}/captures" ]]; then
    echo "Refusing symlinked worker output for worker ${worker}." >&2
    exit 2
  fi
  echo "Worker ${worker}: secure port=${port}, output=${worker_dir}"
  (
    trap - INT TERM
    exec setsid "${RUN_PREFIX[@]}" "${RUNNER_PYTHON}" \
      -m traffic_experiment.traffic_measure.cli run \
      --manifest "${MANIFEST_ABS}" \
      --output-dir "${worker_dir}" \
      --backend local_vllm \
      --base-url "https://${SECURE_PROXY_HOST}:${port}/v1" \
      --model "${VLLM_SERVED_MODEL_NAME}" \
      --samples "${SAMPLES}" \
      --repetitions "${REPETITIONS}" \
      --seed "${RANDOM_SEED}" \
      --max-output-tokens "${MAX_OUTPUT_TOKENS_EFFECTIVE}" \
      --request-timeout-seconds "${REQUEST_TIMEOUT_SECONDS}" \
      --observation-seconds "${OBSERVATION_SECONDS_EFFECTIVE}" \
      --capture-interface "${CAPTURE_INTERFACE_EFFECTIVE}" \
      --capture-filter "${FILTER_PROTOCOL} port ${port}" \
      "${CAPTURE_COMPLETION_ARGS[@]}" \
      --worker-count "${PARALLEL_WORKERS}" \
      --worker-index "${worker}" \
      --worker-gpu-index "${WORKER_GPU_INDEXES[worker]}" \
      --worker-gpu-uuid "${WORKER_GPU_UUIDS[worker]}" \
      --topology-worker-index "${worker}" \
      --transport "${TRANSPORT}" \
      --connection-mode "${CONNECTION_MODE}" \
      --tls-ca-file "${CA_FILE}"
  ) >"${worker_log}" 2>&1 &
  register_child "$!"
done

failed=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[index]}"; then
    failed=1
  fi
  pid_active[index]=0
done
if [[ "${failed}" -ne 0 ]]; then
  echo "A worker failed; inspect ${RUN_DIR}/worker-*.log." >&2
  exit 1
fi

"${RUNNER_PYTHON}" - "${RUN_DIR}" "${RESULTS}" "${OUTPUT_ROOT}/worker-topology.json" <<'PY'
import json
import fcntl
import hashlib
import os
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
topology_path = Path(sys.argv[3])
topology = json.loads(topology_path.read_text(encoding="utf-8"))
worker_count = int(topology["worker_count"])
workers = topology["workers"]


def canonical(row):
    return json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def attempt_identity(row, encoded):
    attempt_id = row.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return ("attempt_id", attempt_id)
    return ("legacy_sha256", hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def register(row, source, seen_attempts, assignments):
    encoded = canonical(row)
    identity = attempt_identity(row, encoded)
    previous = seen_attempts.get(identity)
    if previous is not None:
        if previous != encoded:
            raise SystemExit(f"conflicting duplicate attempt {identity} in {source}")
        return False
    trial = (str(row["request_id"]), int(row["repetition"]))
    worker = int(row["worker_index"])
    if int(row["worker_count"]) != worker_count:
        raise SystemExit(f"{source}: worker_count does not match topology")
    if worker < 0 or worker >= worker_count:
        raise SystemExit(f"{source}: worker_index is outside topology")
    expected = workers[worker]
    if int(row["worker_gpu_index"]) != int(expected["gpu_index"]):
        raise SystemExit(f"{source}: worker GPU index does not match topology")
    if str(row["worker_gpu_uuid"]) != str(expected["gpu_uuid"]):
        raise SystemExit(f"{source}: worker GPU UUID does not match topology")
    if int(row["topology_worker_index"]) != worker:
        raise SystemExit(f"{source}: topology worker index does not match")
    transport = str(row["transport"])
    expected_port = int(expected["secure_ports"][transport])
    if int(row["backend_port"]) != expected_port:
        raise SystemExit(f"{source}: backend port does not match topology")
    assigned = assignments.setdefault(trial, worker)
    if assigned != worker:
        raise SystemExit(f"trial assigned to multiple workers: {trial}")
    seen_attempts[identity] = encoded
    return True


output.parent.mkdir(parents=True, exist_ok=True)
with output.open("a+", encoding="utf-8", newline="\n") as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    stream.seek(0)
    seen_attempts = {}
    assignments = {}
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        register(
            json.loads(line),
            f"{output}:{line_number}",
            seen_attempts,
            assignments,
        )

    new_rows = []
    for worker in range(worker_count):
        path = run_dir / f"worker-{worker}" / "results.jsonl"
        if not path.exists():
            raise SystemExit(f"missing worker results: {path}")
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row["worker_index"]) != worker:
                raise SystemExit(
                    f"{path}:{line_number}: worker_index does not match shard"
                )
            if register(
                row,
                f"{path}:{line_number}",
                seen_attempts,
                assignments,
            ):
                new_rows.append(row)

    new_rows.sort(
        key=lambda row: (
            str(row["sample_id"]),
            str(row["condition"]),
            int(row["repetition"]),
            int(row.get("attempt", 0)),
            str(row.get("attempt_id", "")),
        )
    )
    stream.seek(0, os.SEEK_END)
    for row in new_rows:
        stream.write(canonical(row) + "\n")
    stream.flush()
    os.fsync(stream.fileno())

print(
    f"Appended {len(new_rows)} new attempts to {output}; "
    f"{len(seen_attempts)} attempts recorded"
)
PY

(
  cd /tmp
  "${RUNNER_PYTHON}" -P -m traffic_experiment.traffic_measure.cli analyze \
    --results "${RESULTS}" \
    --output "${RUN_DIR}/traffic_metrics.csv"
)
echo "Parallel ${TRANSPORT} profile complete: ${RESULTS}"
