from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "29_create_privileged_matrix_bundle.sh"
INSTALLER = SCRIPTS / "30_install_privileged_matrix_release.sh"
SUPERVISOR = SCRIPTS / "31_run_privileged_matrix.sh"
SEGMENT_LAUNCHER = SCRIPTS / "32_launch_privileged_matrix_segment.sh"
WAIT_LAUNCHER = SCRIPTS / "33_wait_for_privileged_matrix_gpu.sh"
STATE_TOOL = SCRIPTS / "privileged_matrix_state.py"
REQUEST_TOOL = SCRIPTS / "privileged_matrix_request.py"
CONFIG_TOOL = SCRIPTS / "privileged_matrix_config.py"
EXAMPLE = ROOT / "privileged-matrix.env.example"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matrix_release_is_separate_from_pilot_launcher() -> None:
    pilot = (SCRIPTS / "28_run_privileged_protocol_pilots.sh").read_text()
    matrix = SUPERVISOR.read_text()
    assert "31_run_privileged_matrix" not in pilot
    assert "29_create_privileged_matrix_bundle" not in pilot
    assert "18_run_lab_matrix" not in pilot
    assert "/opt/commu-secure-matrix/releases" in matrix
    assert "/var/lib/commu-secure-matrix/" in matrix
    assert "/var/lib/commu-protocol-pilots/" in matrix
    scan = matrix.index("while IFS='=' read -r inherited_name _; do")
    clean = matrix.index("cd /\n# Bash exports OLDPWD")
    fixed = matrix.index('[[ "${HOME}" == /root')
    assert scan < clean < fixed
    assert "|OLDPWD|" not in matrix
    assert '[[ -z "${engine_args[engine_arg_index]}" ]] || die' in matrix
    assert '"${engine_title}" == \'VLLM::EngineCore\'' in matrix


def test_segment_launcher_has_gpu_portable_relative_lease_interface() -> None:
    text = SEGMENT_LAUNCHER.read_text()
    installer = INSTALLER.read_text()
    assert "new|resume|continue-on-gpu" in text
    assert "--gpu-index N" in text
    assert "--lease 20|115m|2h" in text
    assert "--deadline-epoch" not in text.split("Usage:", 1)[1].split("EOF", 1)[0]
    assert "--gpu-index is an assertion only" in text
    assert "A new run must use a new run ID" in text
    assert "continue-on-gpu is the only cross-GPU path" in text
    assert "--parent-run-id SAFE_ID --parent-run-repository-sha 40_HEX" in text
    assert "privileged_matrix_launch.py" in text
    assert "PROTOCOL_VALIDATION_OK" in text
    assert "matrix-caddy.state" in text
    assert "service-matrix-${RECORD_ID}.state" in text
    timer_arm = text.index('TIMER_ARM_EPOCH="$(/usr/bin/date +%s)"')
    timer_delay = text.index("DELAY_SECONDS=$((CLEANUP_EPOCH - TIMER_ARM_EPOCH))")
    timer_start = text.index('/usr/bin/systemd-run --unit="${TIMER_BASE}"')
    assert timer_arm < timer_delay < timer_start
    assert "recover-open-generation" in text
    assert "/usr/bin/flock -n 8" in text
    assert '"active_config_sha256", "run_plan_sha256"' in text
    assert "stable-runtime-root" in text
    assert '--expected-active-config-sha256 "${PLAN_ACTIVE_CONFIG_SHA256}"' in text
    assert '[[ "${SERVICE_CONFIG_SHA256}" == "${PLAN_ACTIVE_CONFIG_SHA256}" ]]' in text
    assert 'SERVICE_RUNTIME_ROOT="${ATTEMPT_DIR}/runtime"' not in text
    assert 'SEGMENT_LAUNCHER="${RELEASE}/repository/traffic_experiment/scripts/32_launch_privileged_matrix_segment.sh"' in installer
    assert '/usr/bin/chmod 0555 "${SEGMENT_LAUNCHER}"' in installer
    assert '--authorization-cutoff "${AUTHORIZATION_CUTOFF_EPOCH}"' in text


def test_segment_resume_identity_heredoc_is_valid_python() -> None:
    text = SEGMENT_LAUNCHER.read_text()
    block_start = text.index(
        "import json, sys",
        text.index("mapfile -d '' -t plan_fields"),
    )
    block_end = text.index("\nPY\n", block_start)
    program = text[block_start:block_end]
    payload = {
        "repository_sha": "a" * 40,
        "gpu_index": 6,
        "gpu_uuid": "GPU-1234",
        "active_config_sha256": "b" * 64,
        "run_plan_sha256": "c" * 64,
        "parent_matrix_root": "/var/lib/parent",
        "parent_repository_sha": "d" * 40,
        "service_repository_sha": "e" * 40,
    }
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(payload)],
        check=True,
        capture_output=True,
    )
    assert result.stdout.split(b"\0")[:-1] == [
        str(payload[name]).encode("ascii")
        for name in (
            "repository_sha",
            "gpu_index",
            "gpu_uuid",
            "active_config_sha256",
            "run_plan_sha256",
            "parent_matrix_root",
            "parent_repository_sha",
            "service_repository_sha",
        )
    ]


def test_gpu_waiter_is_bounded_detached_and_pinned_to_the_run_plan() -> None:
    text = WAIT_LAUNCHER.read_text()
    installer = INSTALLER.read_text()
    assert "wait-resume" in text
    assert "--authorization-cutoff-epoch EPOCH" in text
    assert "--gpu-index is an assertion" in text
    assert "this script never changes it" in text
    assert '/usr/bin/timeout --signal=TERM --kill-after=2s 10s /usr/bin/nvidia-smi' in text
    assert '"${inventory}" == "${GPU_UUID}"' in text
    assert "vllm-topology" not in text
    assert 'exec 8<>"${TARGET_LOCK}"' in text
    assert '/usr/bin/flock -n 8' in text
    assert 'set -o noclobber; : >"${TARGET_LOCK}"' in text
    assert 'schema=commu-matrix-waiter-active-v1' in text
    assert '"${ACTIVE_REGISTRATION}" == "${WAIT_LOCK_ROOT}/active-${TARGET_ID}.state"' in text
    assert 'die "this immutable run target already has a bounded waiter registration"' in text
    assert "remove_active_registration_locked" in text
    assert 'WAIT_RESUME_EXPIRED_WITHOUT_LAUNCH' in text
    assert 'die "authoritative matrix resume launch failed; it was not retried"' in text
    worker = text[text.index("wait_main() {") : text.index("expire_wait_main() {")]
    assert worker.count('if "${LAUNCHER}" "${launch_args[@]}"; then') == 1
    assert '--authorization-cutoff-epoch "${AUTHORIZATION_CUTOFF_EPOCH}"' in worker
    timer_start = text.index('/usr/bin/systemd-run --unit="${TIMER_BASE}"')
    waiter_start = text.index('/usr/bin/tmux new-session -d -s "${SESSION}"')
    assert timer_start < waiter_start
    runtime_check = text[text.index("shared_runtime_is_free() {") : text.index("wait_main() {")]
    assert 'tcp_service="$(/usr/bin/ss' in runtime_check
    assert 'tcp_protected="$(/usr/bin/ss' in runtime_check
    assert 'udp_protected="$(/usr/bin/ss' in runtime_check
    for port in ("443", "8443", "8444", "8543", "8544"):
        assert f"sport = :{port}" in runtime_check
    assert "sport = :8000 or sport = :8001" in runtime_check
    assert 'netns_rows="$(/usr/bin/ip netns list)" || die' in runtime_check
    assert 'link_rows="$(/usr/bin/ip -o link show)" || die' in runtime_check
    assert "llmhost0" in runtime_check
    assert "| /usr/bin/grep" not in runtime_check
    assert text.index('if [[ "${ACTION}" == _expire-wait ]]') < text.index("release_precheck\ncase")
    assert 'load_expiry_record "${id}"' in text
    expiry = text[text.index("expire_wait_main() {") : text.index('ACTION="${1:-}"')]
    assert "load_wait_record" not in expiry
    assert "outcome.state" in text
    assert "MATRIX_WAIT_RESUME_FINISHED_EARLY" in text
    assert '/usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${WAITERS_ROOT}"' in text
    assert '/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${WAIT_LOG}"' in text
    assert 'resolved="$(/usr/bin/readlink -e -- "${executable}")"' in text
    assert 'stat -c %a -- "${resolved}"' in text
    assert 'WAIT_LAUNCHER="${RELEASE}/repository/traffic_experiment/scripts/33_wait_for_privileged_matrix_gpu.sh"' in installer
    assert '/usr/bin/chmod 0555 "${WAIT_LAUNCHER}"' in installer
    assert '[[ -f "${WAIT_LAUNCHER}" && -x "${WAIT_LAUNCHER}" ]]' in installer


def test_gpu_waiter_can_queue_one_pinned_cross_gpu_continuation() -> None:
    text = WAIT_LAUNCHER.read_text()
    assert "wait-continue-on-gpu" in text
    assert "--parent-run-id SAFE_ID --parent-run-repository-sha 40_HEX" in text
    assert "schema=commu-matrix-waiter-record-v2" in text
    assert "parent_run_id=%s" in text
    assert "parent_run_repository_sha=%s" in text
    assert '"$(sha256_file "${PLAN}")" == "${PLAN_SHA256}"' in text
    assert '[[ "${GPU_UUID}" != "${PARENT_GPU_UUID}" ]]' in text
    assert "find_pinned_parent_plan" in text
    worker = text[text.index("wait_main() {") : text.index("expire_wait_main() {")]
    assert worker.count('if "${LAUNCHER}" "${launch_args[@]}"; then') == 1
    assert "continue-on-gpu --bootstrap-admission-if-missing" in worker
    assert '--parent-run-id "${PARENT_RUN_ID}"' in worker
    assert '--parent-run-repository-sha "${PARENT_RUN_REPOSITORY_SHA}"' in worker
    assert '--authorization-cutoff-epoch "${AUTHORIZATION_CUTOFF_EPOCH}"' in worker


def test_segment_bootstrap_pilot_is_cgroup_bound_and_cleanup_is_ordered() -> None:
    text = SEGMENT_LAUNCHER.read_text()
    segment = text[text.index("segment_main() {") : text.index("expire_main() {")]
    assert "/usr/bin/systemd-run --quiet --wait --pipe --collect --service-type=exec" in segment
    assert "--property=KillMode=control-group" in segment
    assert '"${SELF}" _pilot --record-id "${id}"' in segment

    cleanup = text[
        text.index("cleanup_owned_resources() {") : text.index("recover_generation_if_needed() {")
    ]
    matrix_caddy = cleanup.index('stop_caddy_exact "${caddy_state}"')
    matrix_network = cleanup.index('NETWORK_STATE_FILE="${network_state}"')
    pilot_cleanup = cleanup.index("if ! (cleanup_pilot_bootstrap_resources); then")
    assert matrix_caddy < matrix_network < pilot_cleanup
    assert "if (( pilot_unit_stopped == 1 )); then" in cleanup
    assert "pilot bootstrap unit stop could not be proved; preserving its state" in cleanup
    assert "/usr/bin/flock -w 30 6" in text


def test_gpu_waiter_clean_environment_marker_is_shell_local() -> None:
    text = WAIT_LAUNCHER.read_text()
    marker = 'declare -r COMMU_MATRIX_WAITER_CLEAN_ENV="1"'
    assert f'"${{CLEAN_ENV_DECLARATION}}" != \'{marker}\'' in text
    assert "readonly COMMU_MATRIX_WAITER_CLEAN_ENV=1" in text
    assert f"'{marker}'" in text
    clean_exec = text.split("fi\nPATH=", 1)[0]
    assert "/usr/bin/env -i" in clean_exec
    assert "/usr/bin/bash -p -c" in clean_exec
    assert 'source "${waiter}" "$@"' in clean_exec
    assert 'COMMU_MATRIX_WAITER_CLEAN_ENV=1 \\' not in clean_exec


def test_segment_launcher_clean_environment_marker_is_shell_local() -> None:
    text = SEGMENT_LAUNCHER.read_text()
    marker = 'declare -r COMMU_MATRIX_SEGMENT_CLEAN_ENV="1"'
    assert f'"${{CLEAN_ENV_DECLARATION}}" != \'{marker}\'' in text
    assert "readonly COMMU_MATRIX_SEGMENT_CLEAN_ENV=1" in text
    assert f"'{marker}'" in text
    clean_exec = text.split("fi\nPATH=", 1)[0]
    assert "/usr/bin/env -i" in clean_exec
    assert "/usr/bin/bash -p -c" in clean_exec
    assert 'source "${launcher}" "$@"' in clean_exec
    assert 'COMMU_MATRIX_SEGMENT_CLEAN_ENV=1 \\' not in clean_exec
    assert 'readlink -e -- "${BASH_SOURCE[0]}"' in text


@pytest.mark.parametrize(
    "inherited",
    (
        {"TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%3", "TERM": "screen-256color"},
        {"INVOCATION_ID": "old-invocation", "JOURNAL_STREAM": "8:99", "SYSTEMD_EXEC_PID": "99"},
    ),
)
def test_segment_launcher_child_with_stale_sentinel_resanitizes(
    tmp_path: Path, inherited: dict[str, str]
) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        pytest.skip("the real root-owned launcher bootstrap requires POSIX root")

    text = SEGMENT_LAUNCHER.read_text()
    bootstrap = text.split("\ndie() {", 1)[0]
    harness = tmp_path / "bootstrap.sh"
    harness.write_text(
        bootstrap
        + r'''
for unexpected in TMUX TMUX_PANE TERM INVOCATION_ID JOURNAL_STREAM SYSTEMD_EXEC_PID; do
  [[ -z "${!unexpected+x}" ]] || {
    printf 'unexpected=%s\n' "${unexpected}"
    exit 91
  }
done
[[ "$#" -eq 2 && "$1" == alpha && "$2" == 'two words' ]] || exit 92
/usr/bin/env | /usr/bin/grep -q '^COMMU_MATRIX_SEGMENT_CLEAN_ENV=' && exit 93
printf 'clean-pid=%s sentinel=%s\n' "$$" "${COMMU_MATRIX_SEGMENT_CLEAN_ENV}"
''',
        encoding="utf-8",
    )
    harness.chmod(0o700)
    environment = os.environ.copy()
    environment.update(inherited)
    environment["COMMU_MATRIX_SEGMENT_CLEAN_ENV"] = "1"
    result = subprocess.run(
        ["/usr/bin/bash", "-p", str(harness), "alpha", "two words"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    fields = dict(field.split("=", 1) for field in result.stdout.strip().split())
    assert fields["sentinel"] == "1"
    assert "unsanitized environment variable" not in result.stderr


def test_segment_matrix_log_is_readable_without_exposing_private_record_state() -> None:
    text = SEGMENT_LAUNCHER.read_text()
    assert '/usr/bin/install -d -o root -g "${SERVICE_GID}" -m 0710 "${RECORDS_ROOT}"' in text
    assert '/usr/bin/chown root:"${SERVICE_GID}" "${RECORD_DIR}"' in text
    assert '/usr/bin/chmod 0710 "${RECORD_DIR}"' in text
    assert '"0:${SERVICE_GID}:710"' in text
    assert '/usr/bin/install -o root -g root -m 0600 /dev/null "${RECORD_DIR}/cleanup.lock"' in text
    assert '/usr/bin/chmod 0600 "${RECORD}"' in text
    assert '/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${MATRIX_LOG}"' in text
    assert '/usr/bin/install -o root -g "${SERVICE_GID}" -m 0640 /dev/null "${EXPIRY_LOG}"' in text
    assert '"0:${SERVICE_GID}:640:1"' in text
    assert 'exec >>"${EXPIRY_LOG}" 2>&1' in text


def test_builder_is_platform_stable_credential_free_and_matrix_scoped() -> None:
    text = BUILDER.read_text()
    assert "core.autocrlf=false" in text
    assert "core.eol=lf" in text
    assert text.count("sha256sum --text --") >= 2
    assert "privileged_matrix_config.py" in text
    assert "secure-single-gpu-full-matrix" in text
    assert "30_install_privileged_matrix_release.sh" in text
    assert "LOCAL_VLLM_API_KEY" not in text
    assert "commu-matrix-release." in text


def test_matrix_builder_has_clean_shebang_bytes() -> None:
    data = BUILDER.read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"\xef\xbb\xbf" not in data


def test_installer_requires_existing_shared_lock_and_runtime_requires_admission() -> None:
    text = INSTALLER.read_text()
    supervisor = SUPERVISOR.read_text()
    assert "EXPECTED_REVIEWED_CODE_MANIFEST_SHA256=" in text
    assert "/opt/commu-secure-matrix/releases" in text
    assert "/var/lib/commu-secure-matrix" in text
    assert "pilot-installed shared topology lock is absent or unsafe" in text
    assert "service_state_root" in text
    assert "EXPECTED_GPU_INDEX=" not in text
    assert "EXPECTED_GPU_UUID=" not in text
    assert 'PROTOCOL_ROOT="/var/lib/commu-protocol-pilots/' in supervisor
    assert "verify_admission" in supervisor
    assert "protocol admission is not an immutable root-owned regular file" in supervisor
    assert "LOCK_TMP=" not in text
    assert "31_run_privileged_matrix.sh" in text
    assert 'install -d -o root -g "${SERVICE_GID}" -m 0710' in text


def test_supervisor_holds_locks_for_matrix_and_rechecks_each_cell() -> None:
    text = SUPERVISOR.read_text()
    assert "{check|status|run|resume|continue-on-gpu}" in text
    global_lock = text.index('exec 8<>"${GLOBAL_LOCK_FILE}"')
    state_lock = text.index('SERVICE_LOCK_DIR="${SERVICE_STATE}.lock.d"')
    loop = text.index("for network in baseline rtt realistic")
    assert global_lock < state_lock < loop
    assert text.index("trap matrix_cleanup", state_lock) < loop
    boundary = text[text.index("verify_cell_boundary()") : text.index("run_cell()")]
    for check in (
        "verify_active_service",
        "verify_locked_identity",
        "verify_admission",
        "verify_network_inventory",
        "verify_caddy",
    ):
        assert check in boundary
    run_cell = text[text.index("run_cell()") : text.index("release_precheck\n")]
    assert run_cell.count('verify_cell_boundary "${network}"') == 3
    assert "seal-cell" in run_cell


def test_cross_gpu_continuation_uses_current_runner_and_read_only_parent_ledger() -> None:
    supervisor = SUPERVISOR.read_text()
    launcher = SEGMENT_LAUNCHER.read_text()
    assert "create-continuation-plan" in supervisor
    assert "verify-continuation-plan" in supervisor
    assert 'PRIOR_LEDGER="${MATRIX_ROOT}/PARENT_LEDGER.json"' in supervisor
    assert '--prior-ledger "${19}" --prior-ledger-cell "${20}"' in supervisor
    assert '"${network}/${workload}/${transport}"' in supervisor
    assert 'PILOT_REPOSITORY_SHA="${SOURCE_PARENT_REPOSITORY_SHA}"' in supervisor
    bridge = supervisor[
        supervisor.index("configure_legacy_continuation() {") :
        supervisor.index("check_base_output_hierarchy() {")
    ]
    assert "compare-measurement-payloads" in bridge
    assert "compare-continuation-payloads" in bridge
    assert 'if [[ -n "${SOURCE_PARENT_REPOSITORY_SHA}" ]]; then' in bridge
    assert (
        'ADMISSION_SCRIPT_DIR="${LEGACY_RELEASE_ROOT}/repository/'
        'traffic_experiment/scripts"'
    ) in bridge
    assert "return 0" in bridge
    assert "MEASUREMENT_SCRIPT_DIR=" not in bridge.split(
        'if [[ -n "${SOURCE_PARENT_REPOSITORY_SHA}" ]]; then', 1
    )[1].split("return 0", 1)[0]
    assert '--source-parent-repository-sha "${SERVICE_REPOSITORY_SHA}"' in launcher
    assert 'MEASUREMENT_REPOSITORY_SHA="${REPOSITORY_SHA}"' in launcher
    assert '"${RUNNER}" continue-on-gpu "${runner_args[@]}"' in launcher

    launch_modes = launcher[
        launcher.index('if [[ "${MODE}" == new ]]; then', launcher.index('PLAN=""')) :
        launcher.index('SERVICE_REPOSITORY_SHA=', launcher.index('PLAN=""'))
    ]
    ordinary_new, continuation = launch_modes.split(
        'elif [[ "${MODE}" == continue-on-gpu ]]; then', 1
    )
    assert "PARENT_GPU_UUID" not in ordinary_new
    assert '[[ "${GPU_UUID}" != "${PARENT_GPU_UUID}" ]]' in continuation

    admission = supervisor[
        supervisor.index("verify_admission() {") :
        supervisor.index("STATE_TOOL=", supervisor.index("verify_admission() {"))
    ]
    assert 'source "${ADMISSION_SCRIPT_DIR}/lib.sh"' in admission
    assert 'source "${ADMISSION_SCRIPT_DIR}/protocol_admission.sh"' in admission


def test_caddy_start_waits_for_namespaced_tls_and_http3_readiness() -> None:
    text = SUPERVISOR.read_text()
    start = text[text.index("start_caddy() {") : text.index("stop_caddy() {")]
    assert "/usr/bin/sleep 1" not in start
    assert 'wait_for_caddy_ready "${generated_ca}"' in start
    assert "/usr/sbin/ip netns exec llm-client" in text
    assert '"${RUNNER_PYTHON}" -I "${CADDY_READINESS}"' in text
    assert "--tls-port 8443 --http3-port 8444" in text
    assert 'CADDY_READINESS="${SCRIPT_DIR}/caddy_readiness.py"' in text
    assert '"${CADDY_READINESS}" "${RUNNER_PYTHON}"' in text


def test_caddy_early_exit_still_allows_exact_network_cleanup() -> None:
    text = SUPERVISOR.read_text()
    stop = text[text.index("stop_caddy() {") : text.index("matrix_cleanup() {")]
    exited = stop.index("if caddy_recorded_process_exited; then")
    reap = stop.index('wait "${CADDY_PID}"', exited)
    finalize = stop.index("finalize_stopped_caddy", reap)
    identity = stop.index("if ! caddy_process_matches", finalize)
    assert exited < reap < finalize < identity
    assert "identity is ambiguous; preserving state" in stop

    finalizer = text[
        text.index("finalize_stopped_caddy() {") : text.index("start_caddy() {")
    ]
    assert finalizer.index("caddy_listeners_closed") < finalizer.index(
        '/usr/bin/rm -- "${CADDY_STATE}"'
    )
    for check in (
        "port_closed 8443",
        "udp_port_closed 8444",
        "port_closed 8543",
        "udp_port_closed 8544",
    ):
        assert check in text

    cleanup = text[text.index("matrix_cleanup() {") : text.index("state_args() {")]
    assert "if ! stop_caddy; then" in cleanup
    assert "elif ! cleanup_network; then" in cleanup


def test_caddy_early_exit_cleanup_branch_executes_without_signal() -> None:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.extend(
            [
                str(
                    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                    / "Git/bin/bash.exe"
                ),
                str(
                    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                    / "Git/usr/bin/bash.exe"
                ),
            ]
        )
    bash = None
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run(
                [candidate, "--noprofile", "--norc", "-c", ":"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            bash = candidate
            break
    if bash is None:
        pytest.skip("working Bash is required")

    text = SUPERVISOR.read_text()
    stop = text[text.index("stop_caddy() {") : text.index("matrix_cleanup() {")]
    cleanup = text[text.index("matrix_cleanup() {") : text.index("state_args() {")]
    harness = f"""
set -euo pipefail
CADDY_OWNED=1
CADDY_PID=999999
CADDY_TICKS=123
caddy_recorded_process_exited() {{ return 0; }}
caddy_process_matches() {{ printf 'signal-path-entered\\n'; return 1; }}
finalize_stopped_caddy() {{ CADDY_OWNED=0; printf 'caddy-finalized\\n'; }}
cleanup_network() {{ printf 'network-cleaned\\n'; }}
cleanup() {{ printf 'cleanup-status=%s\\n' "$1"; return "$1"; }}
{stop}
{cleanup}
matrix_cleanup
"""
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "caddy-finalized",
        "network-cleaned",
        "cleanup-status=0",
    ]
    assert "signal-path-entered" not in result.stdout + result.stderr


def test_request_enters_namespace_then_drops_identity_without_lock_or_key() -> None:
    text = SUPERVISOR.read_text()
    child = text[text.index("/usr/sbin/ip netns exec llm-client") :]
    assert child.index("/usr/sbin/ip netns exec llm-client") < child.index(
        "/usr/bin/setpriv --reuid"
    )
    assert "exec 8>&-" in child
    assert '--groups "${18}"' in child
    assert '--groups "$18"' not in child
    assert "--clear-groups" not in child
    assert '"${DUMPCAP_GID}"' in child
    assert 'group_record%%:*}" == wireshark' in text
    assert 'cap_net_admin,cap_net_raw=eip' in text
    assert '"$(/usr/bin/stat -c %u:%a:%h -- "${DUMPCAP}")" == 0:754:1' in text
    assert 'verify_dumpcap_access || die "dumpcap capture identity drifted"' in text
    assert "for (field_number = 1; field_number <= NF; field_number++)" in text
    assert "for (index = 1; index <= NF; index++)" not in text
    assert "/usr/bin/env -i" in child
    assert "API_KEY=" not in text
    assert "Authorization: Bearer" not in text
    request = REQUEST_TOOL.read_text()
    assert "/proc/{pid}/environ" in request
    assert "process_start_ticks(pid) != expected_ticks" in request
    assert "--controller-start-ticks" in request
    assert "LOCAL_VLLM_API_KEY=" in request
    assert "os.environ.pop(name, None)" in request
    assert 'sys.stdin = io.StringIO(credential + "\\n")' in request


def test_dumpcap_group_membership_awk_program_accepts_only_the_exact_gid() -> None:
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("awk is unavailable")
    program = (
        "{for (field_number = 1; field_number <= NF; field_number++) "
        "if ($field_number == gid) found = 1} END {exit !found}"
    )
    accepted = subprocess.run(
        [awk, "-v", "gid=128", program],
        input="1007 128 999\n",
        text=True,
        capture_output=True,
        check=False,
    )
    rejected = subprocess.run(
        [awk, "-v", "gid=128", program],
        input="1007 999\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


def test_matrix_child_reads_the_eighteenth_argument_without_bash_concatenation() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    probe = subprocess.run(
        [bash, "--version"], text=True, capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("bash is unusable")
    result = subprocess.run(
        [
            bash,
            "-c",
            'printf "%s\\n" "${18}"',
            "matrix-child",
            *[str(number) for number in range(1, 18)],
            "128",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "128\n"


def test_fixed_matrix_plan_is_exactly_2808_calls() -> None:
    state = load_module("privileged_matrix_state_test", STATE_TOOL)
    assert state.NETWORKS == ("baseline", "rtt", "realistic")
    assert state.TRANSPORTS == ("tls13", "http3")
    assert state.WORKLOADS == (("qa", 32), ("summary", 20))
    calls = sum(
        samples * state.CONDITIONS * state.REPETITIONS
        for _network in state.NETWORKS
        for _transport in state.TRANSPORTS
        for _workload, samples in state.WORKLOADS
    )
    assert calls == state.EXPECTED_CALLS == 2808


def test_plan_publication_and_exact_verification(tmp_path: Path) -> None:
    if os.name == "nt" or os.getuid() != 0:
        pytest.skip("root POSIX ownership/mode semantics are required")
    state = load_module("privileged_matrix_state_plan_test", STATE_TOOL)
    root = tmp_path / "matrix"
    root.mkdir(mode=0o700)
    parser = state.parser()
    values = [
        "--root", str(root), "--repository-sha", "a" * 40,
        "--pilot-repository-sha", "9" * 40,
        "--release-files-sha256", "b" * 64,
        "--config-sha256", "c" * 64,
        "--admission-sha256", "d" * 64,
        "--service-state-sha256", "e" * 64,
        "--active-config-sha256", "f" * 64,
        "--qa-manifest-sha256", "1" * 64,
        "--summary-manifest-sha256", "2" * 64,
        "--run-id", "main-20260820t180000z",
        "--gpu-index", "4",
        "--gpu-uuid", "GPU-1234",
        "--service-uid", str(os.getuid()),
        "--model", "Qwen/model",
        "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    state.plan_action(parser.parse_args(["create-plan", *values]))
    plan = json.loads((root / "RUN_PLAN.json").read_text())
    assert plan["expected_calls"] == 2808
    assert plan["schema"] == "commu-secure-single-matrix-plan-v2"
    assert "service_state_sha256" not in plan
    assert len(plan["cells"]) == 12
    assert (root / "worker-topology.json").is_file()
    state.plan_action(parser.parse_args(["verify-plan", *values]))
    changed_values = values.copy()
    changed_values[changed_values.index("4")] = "5"
    changed = parser.parse_args(["verify-plan", *changed_values])
    with pytest.raises(ValueError, match="does not match"):
        state.plan_action(changed)


def test_service_generations_are_snapshot_paired_closed_and_hash_chained(
    tmp_path: Path,
) -> None:
    if os.name == "nt" or os.getuid() != 0:
        pytest.skip("root POSIX ownership/mode semantics are required")
    state = load_module("privileged_matrix_generation_test", STATE_TOOL)
    root = tmp_path / "matrix"
    root.mkdir(mode=0o700)
    parser = state.parser()
    plan_values = [
        "--root", str(root), "--repository-sha", "a" * 40,
        "--pilot-repository-sha", "a" * 40,
        "--release-files-sha256", "b" * 64,
        "--config-sha256", "c" * 64,
        "--admission-sha256", "d" * 64,
        "--active-config-sha256", "e" * 64,
        "--qa-manifest-sha256", "1" * 64,
        "--summary-manifest-sha256", "2" * 64,
        "--run-id", "main-generation-test", "--gpu-index", "3",
        "--gpu-uuid", "GPU-1234", "--service-uid", "0",
        "--model", "Qwen/model", "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    state.plan_action(parser.parse_args(["create-plan", *plan_values]))

    snapshot = root / "temporary.state"
    snapshot.write_text("schema=commu-vllm-service-state-v2\nstatus=running\n")
    snapshot.chmod(0o400)
    digest = state.sha256_file(snapshot)
    generation_values = [
        "--root", str(root),
        "--orchestration-repository-sha", "f" * 40,
        "--orchestration-release-sha256", "4" * 64,
        "--service-state-snapshot", str(snapshot),
        "--service-state-sha256", digest,
        "--active-config-sha256", "e" * 64,
        "--admission-sha256", "d" * 64,
    ]
    previous_umask = os.umask(0o077)
    try:
        state.record_generation(
            parser.parse_args(["record-generation", *generation_values])
        )
    finally:
        os.umask(previous_umask)
    records = state.generation_records(root)
    assert len(records) == 1
    assert stat.S_IMODE((root / "SERVICE_GENERATIONS").stat().st_mode) == 0o755
    assert (root / "SERVICE_GENERATIONS/000001.state").is_file()
    with pytest.raises(ValueError, match="still open"):
        state.verify_generations(
            parser.parse_args(["verify-generations", "--root", str(root)])
        )
    with pytest.raises(ValueError, match="still open"):
        state.record_generation(
            parser.parse_args(["record-generation", *generation_values])
        )
    state.close_generation(
        parser.parse_args(
            ["close-generation", "--root", str(root),
             "--service-state-sha256", digest, "--outcome", "interrupted"]
        )
    )
    first_closure = state.sha256_file(
        root / "SERVICE_GENERATIONS/000001.closed.json"
    )
    state.record_generation(parser.parse_args(["record-generation", *generation_values]))
    second = state.generation_records(root)[1][1]
    assert second["previous_record_sha256"] == first_closure


def test_forced_teardown_can_recover_only_the_matching_open_generation(
    tmp_path: Path,
) -> None:
    if os.name == "nt" or os.getuid() != 0:
        pytest.skip("root POSIX ownership/mode semantics are required")
    state = load_module("privileged_matrix_generation_recovery_test", STATE_TOOL)
    root = tmp_path / "matrix"
    root.mkdir(mode=0o700)
    parser = state.parser()
    orchestration_sha = "f" * 40
    plan_values = [
        "--root", str(root), "--repository-sha", "a" * 40,
        "--pilot-repository-sha", "a" * 40,
        "--release-files-sha256", "b" * 64,
        "--config-sha256", "c" * 64,
        "--admission-sha256", "d" * 64,
        "--active-config-sha256", "e" * 64,
        "--qa-manifest-sha256", "1" * 64,
        "--summary-manifest-sha256", "2" * 64,
        "--run-id", "forced-teardown-recovery", "--gpu-index", "3",
        "--gpu-uuid", "GPU-1234", "--service-uid", "0",
        "--model", "Qwen/model", "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    state.plan_action(parser.parse_args(["create-plan", *plan_values]))
    snapshot = root / "temporary.state"
    snapshot.write_text("schema=commu-vllm-service-state-v2\nstatus=running\n")
    snapshot.chmod(0o400)
    digest = state.sha256_file(snapshot)
    state.record_generation(
        parser.parse_args(
            [
                "record-generation", "--root", str(root),
                "--orchestration-repository-sha", orchestration_sha,
                "--orchestration-release-sha256", "4" * 64,
                "--service-state-snapshot", str(snapshot),
                "--service-state-sha256", digest,
                "--active-config-sha256", "e" * 64,
                "--admission-sha256", "d" * 64,
            ]
        )
    )
    wrong = parser.parse_args(
        [
            "recover-open-generation", "--root", str(root),
            "--orchestration-repository-sha", "9" * 40,
        ]
    )
    with pytest.raises(ValueError, match="different orchestration"):
        state.recover_open_generation(wrong)
    recover = parser.parse_args(
        [
            "recover-open-generation", "--root", str(root),
            "--orchestration-repository-sha", orchestration_sha,
        ]
    )
    state.recover_open_generation(recover)
    closure = json.loads(
        (root / "SERVICE_GENERATIONS/000001.closed.json").read_text()
    )
    assert closure["outcome"] == "interrupted"
    state.verify_generations(
        parser.parse_args(["verify-generations", "--root", str(root)])
    )
    with pytest.raises(ValueError, match="already closed"):
        state.recover_open_generation(recover)


def test_legacy_generation_zero_uses_plan_hash_with_preexisting_empty_directory(
    tmp_path: Path,
) -> None:
    if os.name == "nt" or os.getuid() != 0:
        pytest.skip("root POSIX ownership/mode semantics are required")
    state = load_module("privileged_matrix_legacy_generation_test", STATE_TOOL)
    root = tmp_path / "matrix"
    root.mkdir(mode=0o700)
    parser = state.parser()
    plan_values = [
        "--root", str(root), "--repository-sha", "a" * 40,
        "--pilot-repository-sha", "a" * 40,
        "--release-files-sha256", "b" * 64,
        "--config-sha256", "c" * 64,
        "--admission-sha256", "d" * 64,
        "--active-config-sha256", "e" * 64,
        "--qa-manifest-sha256", "1" * 64,
        "--summary-manifest-sha256", "2" * 64,
        "--run-id", "main-legacy-generation-test", "--gpu-index", "3",
        "--gpu-uuid", "GPU-1234", "--service-uid", "0",
        "--model", "Qwen/model", "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    args = parser.parse_args(["create-plan", *plan_values])
    plan = state.expected_plan(args)
    plan["schema"] = state.LEGACY_SCHEMA
    plan["service_state_sha256"] = "9" * 64
    state.publish(root / "worker-topology.json", state.expected_topology(args))
    state.publish(root / "RUN_PLAN.json", plan)
    generations = root / "SERVICE_GENERATIONS"
    generations.mkdir(mode=0o755)
    generations.chmod(0o755)

    snapshot = root / "temporary.state"
    snapshot.write_text("schema=commu-vllm-service-state-v2\nstatus=running\n")
    snapshot.chmod(0o400)
    digest = state.sha256_file(snapshot)
    state.record_generation(
        parser.parse_args(
            [
                "record-generation", "--root", str(root),
                "--orchestration-repository-sha", "f" * 40,
                "--orchestration-release-sha256", "4" * 64,
                "--service-state-snapshot", str(snapshot),
                "--service-state-sha256", digest,
                "--active-config-sha256", "e" * 64,
                "--admission-sha256", "d" * 64,
            ]
        )
    )
    first = state.generation_records(root)[0][1]
    assert first["predecessor_service_state_sha256"] == "9" * 64


def test_legacy_verification_preserves_v1_plan_bytes_and_inode(tmp_path: Path) -> None:
    if os.name == "nt" or os.getuid() != 0:
        pytest.skip("root POSIX ownership/mode semantics are required")
    state = load_module("privileged_matrix_legacy_test", STATE_TOOL)
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    parser = state.parser()
    old_sha = "a" * 40
    release_sha = "b" * 64
    config_sha = "c" * 64
    admission_sha = "d" * 64
    values = [
        "--root", str(root), "--repository-sha", old_sha,
        "--pilot-repository-sha", old_sha,
        "--release-files-sha256", release_sha,
        "--config-sha256", config_sha,
        "--admission-sha256", admission_sha,
        "--active-config-sha256", "e" * 64,
        "--qa-manifest-sha256", "1" * 64,
        "--summary-manifest-sha256", "2" * 64,
        "--run-id", "legacy-main", "--gpu-index", "3",
        "--gpu-uuid", "GPU-1234", "--service-uid", "0",
        "--model", "Qwen/model", "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    args = parser.parse_args(
        ["verify-legacy-plan", *values,
         "--legacy-repository-sha", old_sha,
         "--legacy-release-files-sha256", release_sha,
         "--legacy-config-sha256", config_sha,
         "--legacy-admission-sha256", admission_sha]
    )
    plan = state.expected_plan(args)
    plan["schema"] = state.LEGACY_SCHEMA
    plan["service_state_sha256"] = "9" * 64
    state.publish(root / "worker-topology.json", state.expected_topology(args))
    state.publish(root / "RUN_PLAN.json", plan)
    before = (root / "RUN_PLAN.json").read_bytes()
    inode = (root / "RUN_PLAN.json").stat().st_ino
    state.legacy_plan_action(args)
    assert (root / "RUN_PLAN.json").read_bytes() == before
    assert (root / "RUN_PLAN.json").stat().st_ino == inode


def test_existing_check_verifies_resumability_and_legacy_bridge_is_explicit() -> None:
    text = SUPERVISOR.read_text()
    assert "--legacy-run-repository-sha 40_HEX" in text
    assert "configure_legacy_continuation" in text
    assert "legacy/current measurement payloads are not byte-identical" in text
    assert "INSTALLED_RUNTIME_FILES.sha256" in text
    assert "compare-measurement-payloads" in text
    flow = text[text.index('mapfile -d \'\' -t PLAN_ARGS') :]
    assert flow.index("verify_selected_plan") < flow.index(
        "PRIVILEGED_MATRIX_PRECHECK_OK"
    )
    assert flow.index("open_service_generation") < flow.index(
        "trap matrix_cleanup"
    )
    assert flow.index("close_service_generation ready-to-seal") < flow.index(
        'seal-matrix --root "${MATRIX_ROOT}"'
    )


def test_measurement_payload_comparison_ignores_only_interpreter_caches(
    tmp_path: Path,
) -> None:
    state = load_module("privileged_matrix_payload_test", STATE_TOOL)
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()

    entries = {
        relative: f"{number:064x}"
        for number, relative in enumerate(
            sorted(state.MEASUREMENT_PAYLOAD_FILES), 1
        )
    }
    entries["repository/traffic_experiment/traffic_measure/client.py"] = "a" * 64
    entries["wheelhouse/httpx-1.0-py3-none-any.whl"] = "b" * 64

    def write_manifest(root: Path, values: dict[str, str]) -> None:
        text = "".join(
            f"{digest}  ./{relative}\n"
            for relative, digest in sorted(values.items())
        )
        (root / "RELEASE_FILES.sha256").write_text(text, encoding="utf-8")

    old_entries = {
        **entries,
        "repository/traffic_experiment/traffic_measure/__pycache__/client.cpython-312.pyc": "c" * 64,
    }
    new_entries = {
        **entries,
        "repository/traffic_experiment/traffic_measure/__pycache__/client.cpython-312.pyc": "d" * 64,
    }
    write_manifest(old, old_entries)
    write_manifest(new, new_entries)
    args = state.parser().parse_args(
        [
            "compare-measurement-payloads",
            "--legacy-release-root", str(old),
            "--current-release-root", str(new),
        ]
    )
    state.compare_measurement_payloads(args)

    new_entries["repository/traffic_experiment/traffic_measure/client.py"] = "e" * 64
    write_manifest(new, new_entries)
    with pytest.raises(
        ValueError,
        match="legacy/current measurement payloads are not byte-identical",
    ):
        state.compare_measurement_payloads(args)


def test_installer_smoke_check_disables_bytecode_writes_explicitly() -> None:
    text = INSTALLER.read_text()
    assert '"${RUNTIME_PYTHON}" -B -I -P -c' in text


def test_config_example_is_secret_free_and_fixed() -> None:
    text = EXAMPLE.read_text()
    assert "LOCAL_VLLM_API_KEY=" not in text
    assert text.count("@REPOSITORY_SHA@") == 3
    for assignment in (
        'CUDA_VISIBLE_DEVICES="@GPU_INDEX@"',
        'PARALLEL_WORKERS="1"',
        'LAB_NETWORKS="baseline rtt realistic"',
        'LAB_QA_SAMPLES="32"',
        'LAB_SUMMARY_SAMPLES="20"',
        'LAB_REPETITIONS="3"',
        'LAB_TRANSPORTS="tls13 http3"',
        'LAB_WORKLOADS="qa summary"',
    ):
        assert assignment in text


def test_config_validator_accepts_only_exact_matrix(tmp_path: Path) -> None:
    config = load_module("privileged_matrix_config_test", CONFIG_TOOL)
    repository_sha = "a" * 40
    text = EXAMPLE.read_text().replace("@REPOSITORY_SHA@", repository_sha)
    text = text.replace('VLLM_MODEL_REVISION=""', f'VLLM_MODEL_REVISION="{"b" * 40}"')
    text = text.replace('MANIFEST_SHA256=""', f'MANIFEST_SHA256="{"c" * 64}"')
    text = text.replace(
        'SUMMARY_MANIFEST_SHA256=""',
        f'SUMMARY_MANIFEST_SHA256="{"d" * 64}"',
    )
    path = tmp_path / "matrix.env"
    path.write_text(text)
    values = config.parse_config(path)
    config.validate_release_config(values, repository_sha, None)
    rendered = tmp_path / "gpu4.env"
    config.materialize(
        path,
        rendered,
        repository_sha,
        "4",
        "GPU-1234",
    )
    rendered_values = config.parse_config(rendered)
    assert rendered_values["CUDA_VISIBLE_DEVICES"] == "4"
    assert rendered_values["RUNS_ROOT"] == (
        f"/var/lib/commu-secure-matrix/{repository_sha}/gpu-4-GPU-1234/runs"
    )
    assert rendered_values["CADDY_RUN_DIR"] == (
        f"/var/lib/commu-secure-matrix/{repository_sha}/gpu-4-GPU-1234/caddy"
    )
    values["LAB_NETWORKS"] = "baseline realistic"
    with pytest.raises(config.ConfigError, match="LAB_NETWORKS"):
        config.validate_release_config(values, repository_sha, None)


def test_supervisor_binds_selected_gpu_to_output_plan_and_admission() -> None:
    text = SUPERVISOR.read_text()
    assert "--service-state /absolute/path/to/service.state --run-id SAFE_ID" in text
    assert 'scope="gpu-${gpu_index}-${gpu_uuid}"' in text
    assert 'OUTPUT_ROOT="${BASE_OUTPUT_ROOT}/${scope}"' in text
    assert 'PROTOCOL_ROOT="/var/lib/commu-protocol-pilots/${PILOT_REPOSITORY_SHA}/${scope}/runs/protocol_validation"' in text
    admission = text[text.index("verify_admission() {") : text.index("STATE_TOOL=")]
    source_lib = admission.index('source "${ADMISSION_SCRIPT_DIR}/lib.sh"')
    rebase_runs = admission.index('RUNS_ROOT="${protocol_runs}"')
    rebase_manifest = admission.index('MANIFEST_PATH="${pilot_manifest}"')
    verify_marker = admission.index("verify_protocol_admission")
    assert source_lib < rebase_runs < rebase_manifest < verify_marker
    assert 'pilot_release_root="/opt/commu-protocol-pilots/releases/${PILOT_REPOSITORY_SHA}"' in admission
    assert '[[ "$(sha256_file "${pilot_manifest}")" == "$(config_value "${CONFIG}" MANIFEST_SHA256)" ]]' in admission
    assert '--gpu-index "${EXPECTED_GPU_INDEX}"' in text
    assert '--worker-gpu-index "${17}"' in text
    assert 'MATRIX_ROOT="${OUTPUT_ROOT}/runs/${RUN_ID}"' in text
    assert '--run-id "${RUN_ID}"' in text
    assert "worker-gpu-index 2" not in text


def test_both_root_installers_are_outside_reviewed_code_manifest() -> None:
    pilot_builder = (SCRIPTS / "26_create_privileged_pilot_bundle.sh").read_text()
    pilot_installer = (SCRIPTS / "27_install_privileged_pilot_release.sh").read_text()
    matrix_builder = BUILDER.read_text()
    for installer in (
        "27_install_privileged_pilot_release.sh",
        "30_install_privileged_matrix_release.sh",
    ):
        assert installer in pilot_builder
        assert installer in matrix_builder
        assert installer in pilot_installer
        assert installer in INSTALLER.read_text()


def test_sealing_is_no_follow_hardlink_checked_and_atomic() -> None:
    text = STATE_TOOL.read_text()
    assert "os.O_NOFOLLOW" in text
    assert "st_nlink != 1" in text
    assert 'root / ".sealing"' in text
    assert "os.rename(original_cell, staged)" in text
    assert "os.link(temporary, path, follow_symlinks=False)" in text
    assert "incomplete sealing state exists" in text
    assert "capture != resolved_capture" in text
    assert "sha256_file(actual_capture) != digest" in text
