#!/usr/bin/env bash
# Shared immutable protocol-pilot admission helpers.
#
# Source this after scripts/lib.sh so EXPERIMENT_ROOT, RUNS_ROOT, manifest/model
# settings, and absolute_from_experiment are available.

protocol_stack_sha256() {
  local -a protocol_files=(
    configs/Caddyfile
    scripts/08_run_transport_profile.sh
    scripts/08_run_transport_profile_parallel.sh
    scripts/11_network_condition.sh
    scripts/14_run_warm_session.sh
    scripts/17_lab_preflight.sh
    scripts/18_run_lab_matrix.sh
    scripts/19_run_lab_sessions.sh
    scripts/22_validate_protocol_pilots.sh
    scripts/protocol_admission.sh
    traffic_measure/backends.py
    traffic_measure/capture.py
    traffic_measure/cli.py
    traffic_measure/http3_client.py
    traffic_measure/pilot_evidence.py
    traffic_measure/runner.py
    traffic_measure/session_timeline.py
    traffic_measure/vllm_metrics.py
  )
  (
    cd "${EXPERIMENT_ROOT}"
    sha256sum "${protocol_files[@]}"
  ) | sha256sum | awk '{print $1}'
}

protocol_marker_value() {
  local marker="$1" key="$2"
  awk -F= -v key="${key}" \
    '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${marker}"
}

require_protocol_marker_match() {
  local marker="$1" key="$2" expected="$3"
  local actual
  actual="$(protocol_marker_value "${marker}" "${key}")"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "Protocol marker mismatch for ${key}: expected '${expected}', got '${actual:-missing}'." >&2
    return 1
  fi
}

protocol_validation_root_path() {
  local run_root requested parent name
  run_root="$(realpath -m -- "$(absolute_from_experiment "${RUNS_ROOT}")")"
  requested="${PROTOCOL_VALIDATION_ROOT:-${run_root}/protocol_validation}"
  if [[ "${requested}" != /* ]]; then
    requested="$(absolute_from_experiment "${requested}")"
  fi
  requested="$(realpath -m -- "${requested}")"
  parent="$(dirname -- "${requested}")"
  name="$(basename -- "${requested}")"
  if [[ "${parent}" != "${run_root}" ||
    ! "${name}" =~ ^protocol_validation(-[A-Za-z0-9][A-Za-z0-9._-]*)?$ ]]; then
    echo "Protocol-validation root must be a direct, named generation under ${run_root}." >&2
    return 1
  fi
  printf '%s\n' "${requested}"
}

protocol_validation_marker_path() {
  local root marker
  root="$(protocol_validation_root_path)" || return 1
  marker="${PROTOCOL_VALIDATION_MARKER:-${root}/PROTOCOL_VALIDATION_OK}"
  if [[ "${marker}" != /* ]]; then
    marker="$(absolute_from_experiment "${marker}")"
  fi
  marker="$(realpath -m -- "${marker}")"
  if [[ "$(dirname -- "${marker}")" != "${root}" ||
    "$(basename -- "${marker}")" != "PROTOCOL_VALIDATION_OK" ]]; then
    echo "Protocol-validation marker must be PROTOCOL_VALIDATION_OK inside ${root}." >&2
    return 1
  fi
  printf '%s\n' "${marker}"
}

protocol_pilot_capture_filter() {
  local transport="$1"
  case "${transport}" in
    tls13) printf 'tcp port 8443\n' ;;
    http3) printf '(udp port 8444 or tcp port 8444)\n' ;;
    *)
      echo "Unsupported protocol-pilot transport: ${transport}" >&2
      return 2
      ;;
  esac
}

protocol_pilot_evidence() {
  local action="$1" marker="$2" transport="$3"
  local results="${4:-}"
  local -a results_args=()
  if [[ -n "${results}" ]]; then
    results_args=(--results "${results}")
  fi
  "${RUNNER_PYTHON}" \
    -m traffic_experiment.traffic_measure.pilot_evidence "${action}" \
    --marker "${marker}" \
    "${results_args[@]}" \
    --manifest "$(absolute_from_experiment "${MANIFEST_PATH}")" \
    --transport "${transport}" \
    --model "${VLLM_SERVED_MODEL_NAME}" \
    --manifest-sha256 "${MANIFEST_SHA256,,}" \
    --model-revision "${VLLM_MODEL_REVISION}" \
    --protocol-stack-sha256 "$(protocol_stack_sha256)" \
    --network-mtu "${NETWORK_MTU}" \
    --backend-ip "${SECURE_PROXY_HOST}" \
    --capture-interface "${CLIENT_VETH:-llmclient0}" \
    --capture-filter "$(protocol_pilot_capture_filter "${transport}")" \
    --seed "${RANDOM_SEED}" \
    --max-output-tokens 4096
}

verify_protocol_pilot_reference() {
  local marker="$1" field_prefix="$2" transport="$3"
  local evidence_marker expected_sha actual_sha
  evidence_marker="$(protocol_marker_value "${marker}" "${field_prefix}_marker")"
  expected_sha="$(
    protocol_marker_value "${marker}" "${field_prefix}_marker_sha256"
  )"
  if [[ ! "${expected_sha}" =~ ^[0-9a-f]{64}$ ||
    ! -f "${evidence_marker}" || -L "${evidence_marker}" ]]; then
    echo "Protocol marker has invalid ${field_prefix} evidence metadata." >&2
    return 1
  fi
  actual_sha="$(sha256sum "${evidence_marker}" | awk '{print $1}')"
  if [[ "${actual_sha}" != "${expected_sha}" ]]; then
    echo "Protocol marker ${field_prefix} evidence hash no longer matches." >&2
    return 1
  fi
  protocol_pilot_evidence verify "${evidence_marker}" "${transport}" \
    >/dev/null
}

verify_protocol_admission() {
  local marker
  marker="$(protocol_validation_marker_path)"
  if [[ ! -f "${marker}" || -L "${marker}" ]]; then
    echo "Missing successful protocol-pilot marker: ${marker}" >&2
    echo "Run scripts/22_validate_protocol_pilots.sh before measured runs." >&2
    return 1
  fi
  if [[ "$(stat -c %u "${marker}")" != "${EUID}" ||
    "$(stat -c %a "${marker}")" != "444" ]]; then
    echo "Protocol-pilot marker must be owned by UID ${EUID} with mode 0444: ${marker}" >&2
    return 1
  fi
  require_protocol_marker_match \
    "${marker}" schema commu-protocol-admission-v1
  require_protocol_marker_match "${marker}" status success
  require_protocol_marker_match \
    "${marker}" qa_manifest_sha256 "${MANIFEST_SHA256,,}"
  require_protocol_marker_match \
    "${marker}" summary_manifest_sha256 "${SUMMARY_MANIFEST_SHA256,,}"
  require_protocol_marker_match \
    "${marker}" model_revision "${VLLM_MODEL_REVISION}"
  require_protocol_marker_match "${marker}" model_name "${VLLM_MODEL}"
  require_protocol_marker_match "${marker}" network_mtu "${NETWORK_MTU}"
  require_protocol_marker_match \
    "${marker}" caddy_version "$(caddy version 2>&1)"
  require_protocol_marker_match \
    "${marker}" protocol_stack_sha256 "$(protocol_stack_sha256)"
  verify_protocol_pilot_reference "${marker}" tls_pilot tls13
  verify_protocol_pilot_reference "${marker}" http3_pilot http3
  echo "Protocol-pilot admission verified: ${marker}"
}

write_protocol_success_marker() {
  local marker="${1:-$(protocol_validation_marker_path)}"
  local tls_evidence_marker="${2:?TLS pilot evidence marker is required}"
  local http3_evidence_marker="${3:?HTTP/3 pilot evidence marker is required}"
  if [[ -e "${marker}" || -L "${marker}" ]]; then
    echo "Refusing to overwrite protocol-validation marker ${marker}; preserve or move it before revalidation" >&2
    return 1
  fi

  local marker_dir marker_tmp qa_sha summary_sha caddy_version stack_sha
  local tls_evidence_sha http3_evidence_sha
  marker_dir="$(dirname -- "${marker}")"
  mkdir -p "${marker_dir}"
  marker_tmp="$(mktemp "${marker_dir}/.protocol-validation.XXXXXX")"
  if ! qa_sha="$(
    sha256sum "$(absolute_from_experiment "${MANIFEST_PATH}")" |
      awk '{print $1}'
  )" ||
    ! summary_sha="$(
      sha256sum "$(absolute_from_experiment "${SUMMARY_MANIFEST_PATH}")" |
        awk '{print $1}'
    )" ||
    ! caddy_version="$(caddy version 2>&1)" ||
    ! stack_sha="$(protocol_stack_sha256)" ||
    ! protocol_pilot_evidence verify \
      "${tls_evidence_marker}" tls13 >/dev/null ||
    ! protocol_pilot_evidence verify \
      "${http3_evidence_marker}" http3 >/dev/null ||
    ! tls_evidence_sha="$(
      sha256sum "${tls_evidence_marker}" | awk '{print $1}'
    )" ||
    ! http3_evidence_sha="$(
      sha256sum "${http3_evidence_marker}" | awk '{print $1}'
    )"; then
    rm -f -- "${marker_tmp}"
    return 1
  fi

  if ! {
    printf 'schema=commu-protocol-admission-v1\n'
    printf 'status=success\n'
    printf 'qa_manifest_sha256=%s\n' "${qa_sha}"
    printf 'summary_manifest_sha256=%s\n' "${summary_sha}"
    printf 'model_revision=%s\n' "${VLLM_MODEL_REVISION}"
    printf 'model_name=%s\n' "${VLLM_MODEL}"
    printf 'network_mtu=%s\n' "${NETWORK_MTU}"
    printf 'caddy_version=%s\n' "${caddy_version}"
    printf 'protocol_stack_sha256=%s\n' "${stack_sha}"
    printf 'tls_pilot_marker=%s\n' "${tls_evidence_marker}"
    printf 'tls_pilot_marker_sha256=%s\n' "${tls_evidence_sha}"
    printf 'http3_pilot_marker=%s\n' "${http3_evidence_marker}"
    printf 'http3_pilot_marker_sha256=%s\n' "${http3_evidence_sha}"
    printf 'validated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${marker_tmp}"; then
    rm -f -- "${marker_tmp}"
    return 1
  fi
  if ! chmod 0444 "${marker_tmp}" || ! mv "${marker_tmp}" "${marker}"; then
    rm -f -- "${marker_tmp}"
    return 1
  fi
}
