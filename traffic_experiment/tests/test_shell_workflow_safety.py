from __future__ import annotations

import os
import runpy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _working_posix_bash() -> str:
    if os.name != "posix":
        pytest.skip("shell workflow execution requires POSIX")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("working Bash is required")
    try:
        probe = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", ":"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("working Bash is required")
    if probe.returncode != 0:
        pytest.skip("working Bash is required")
    return bash


def test_namespaced_capture_uses_and_validates_client_visible_interface():
    preliminary = _script("12_run_preliminary_resumable.sh")
    sessions = _script("19_run_lab_sessions.sh")
    for name in (
        "08_run_transport_profile.sh",
        "08_run_transport_profile_parallel.sh",
        "14_run_warm_session.sh",
    ):
        script = _script(name)
        assert 'CAPTURE_INTERFACE_OVERRIDE:-${CLIENT_VETH:-llmclient0}' in script
        assert 'ip link show dev "${CAPTURE_INTERFACE_EFFECTIVE}"' in script

    assert 'CAPTURE_INTERFACE_OVERRIDE="${CAPTURE_IF}"' in preliminary
    assert 'CAPTURE_IF="${CLIENT_IF}"' in preliminary
    assert 'CAPTURE_INTERFACE_OVERRIDE="${CLIENT_IF}"' in sessions
    assert 'CAPTURE_INTERFACE_OVERRIDE="${HOST_IF}"' not in sessions


def test_caddy_cleanup_waits_after_kill_and_verifies_listener_closure():
    expectations = {
        "12_run_preliminary_resumable.sh": (
            "wait_for_owned_caddy_exit",
            "caddy_listeners_closed",
        ),
        "22_validate_protocol_pilots.sh": (
            "wait_for_owned_caddy_exit",
            "caddy_listeners_closed",
        ),
        "23_server_physical_listener.sh": (
            "_wait_for_verified_caddy_exit",
            "_listener_ports_closed",
        ),
    }
    for name, (wait_helper, listener_helper) in expectations.items():
        script = _script(name)
        kill_index = script.index("kill -KILL")
        second_wait_index = script.index(wait_helper, kill_index)
        listener_index = script.index(listener_helper, second_wait_index)
        assert kill_index < second_wait_index < listener_index
        assert "did not exit" in script

    listener = _script("23_server_physical_listener.sh")
    assert 'for port in "${PHYSICAL_TCP_PORTS[@]}"' in listener
    assert 'for port in "${PHYSICAL_UDP_PORTS[@]}"' in listener
    assert "load_recorded_listener_topology" in listener
    start_body = listener[
        listener.index("start_listener() {") : listener.index("stop_listener() {")
    ]
    assert start_body.index("load_recorded_listener_topology") < start_body.index(
        'rm -f "${PID_FILE}" "${STATE_FILE}"'
    )
    assert start_body.count("_listener_ports_closed") == 2
    assert "state exists without a PID" in start_body
    assert "_discover_physical_ip || true" in listener


def test_main_matrix_requires_matching_protocol_success_marker():
    validation = _script("22_validate_protocol_pilots.sh")
    matrix = _script("18_run_lab_matrix.sh")
    sessions = _script("19_run_lab_sessions.sh")
    admission_helper = " ".join(
        _script("protocol_admission.sh").replace("\\\n", " ").split()
    )

    assert "write_protocol_success_marker" in validation
    assert "status=success" in admission_helper
    assert "protocol_stack_sha256=" in admission_helper
    assert "PROTOCOL_VALIDATION_OK" in admission_helper

    for measured_script in (matrix, sessions):
        preflight_index = measured_script.index(
            '"${SCRIPT_DIR}/17_lab_preflight.sh"'
        )
        admission_index = measured_script.index(
            "verify_protocol_admission",
            preflight_index,
        )
        network_index = measured_script.index("for network in", admission_index)
        assert preflight_index < admission_index < network_index
    for key in (
        "qa_manifest_sha256",
        "summary_manifest_sha256",
        "model_revision",
        "caddy_version",
        "protocol_stack_sha256",
    ):
        assert (
            f'require_protocol_marker_match "${{marker}}" {key}'
            in admission_helper
        )


def test_manifest_entrypoints_refuse_overwrite_and_preflight_checks_digests():
    for name in (
        "02_prepare_manifest.sh",
        "02_prepare_manifest_parallel.sh",
        "02_prepare_summary_manifest.sh",
    ):
        script = _script(name)
        assert "Refusing to overwrite" in script
        assert "sha256sum" in script

    preflight = _script("17_lab_preflight.sh")
    assert "MANIFEST_SHA256 must be the expected 64-character SHA-256" in preflight
    assert "SUMMARY_MANIFEST_SHA256 must be the expected 64-character SHA-256" in preflight
    assert '"${QA_MANIFEST_ACTUAL_SHA}" != "${MANIFEST_SHA256,,}"' in preflight
    assert (
        '"${SUMMARY_MANIFEST_ACTUAL_SHA}" != "${SUMMARY_MANIFEST_SHA256,,}"'
        in preflight
    )


def test_manifest_entrypoints_select_isolated_interpreters_with_safe_imports():
    library = _script("lib.sh")
    assert '.venv-compression/bin/python' in library
    assert "select_manifest_python()" in library
    assert "run_python_safely()" in library
    assert "env -u PYTHONHOME" in library
    assert 'PYTHONPATH="${REPOSITORY_ROOT}"' in library
    assert "PYTHONSAFEPATH=1" in library
    assert '"${python_bin}" -P' in library
    assert "require_compressor_device_for_conditions()" in library

    for name in (
        "02_prepare_manifest.sh",
        "02_prepare_manifest_parallel.sh",
        "02_prepare_summary_manifest.sh",
    ):
        script = _script(name)
        assert "select_manifest_python" in script
        assert "PREPARATION_PYTHON" in script
        assert '"${PREPARATION_PYTHON}"' in script
        assert "require_compressor_device_for_conditions" in script

    parallel = _script("02_prepare_manifest_parallel.sh")
    assert "exec setsid env -u PYTHONHOME" in parallel
    assert 'PYTHONPATH="${REPOSITORY_ROOT}"' in parallel
    assert "PYTHONSAFEPATH=1" in parallel
    assert '"${PREPARATION_PYTHON}" -P' in parallel
    assert 'run_python_safely "${RUNNER_PYTHON}"' in parallel
    assert 'if [[ "${COMPRESSOR_DEVICE}" == "cpu" ]]' in parallel
    assert "unset CUDA_VISIBLE_DEVICES" in parallel


def test_parallel_manifest_cpu_gate_precedes_interpreter_and_child_launches():
    parallel = _script("02_prepare_manifest_parallel.sh")
    gate = parallel.index('if [[ "${COMPRESSOR_DEVICE}" == "cpu" ]]')
    refusal = parallel.index("CPU parallel manifest preparation is disabled", gate)
    exit_gate = parallel.index("exit 2", refusal)
    interpreter = parallel.index("PREPARATION_PYTHON=", exit_gate)
    first_launch = parallel.index("prepare_shard 0", interpreter)
    assert gate < refusal < exit_gate < interpreter < first_launch
    assert "scripts/02_prepare_manifest.sh" in parallel


def test_parallel_manifest_cpu_mode_refuses_without_starting_compressor(tmp_path: Path):
    bash = _working_posix_bash()

    data_dir = tmp_path / "locomo"
    data_dir.mkdir()
    marker = tmp_path / "compressor-started"
    fake_python = tmp_path / "compression-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {str(marker)!r}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env_file = tmp_path / "server.env"
    env_file.write_text(
        f'LOCOMO_DATA_DIR={str(data_dir)!r}\n'
        'COMPRESSOR_DEVICE="cpu"\n'
        'COMPRESSOR_MODEL="NousResearch/Llama-2-7b-hf"\n'
        'MANIFEST_PATH="artifacts/test.jsonl"\n'
        'RANDOM_SEED="42"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "EXPERIMENT_ENV_FILE": str(env_file),
            "COMPRESSION_PYTHON": str(fake_python),
        }
    )
    completed = subprocess.run(
        [bash, str(SCRIPTS / "02_prepare_manifest_parallel.sh")],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert "CPU parallel manifest preparation is disabled" in completed.stderr
    assert "02_prepare_manifest.sh" in completed.stderr
    assert not marker.exists()


def test_gpu_compressor_gate_binds_approval_to_canonical_interpreter(tmp_path: Path):
    bash = _working_posix_bash()
    selected = tmp_path / "selected-python"
    other = tmp_path / "other-python"
    alias = tmp_path / "approved-python"
    for executable in (selected, other):
        executable.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    alias.symlink_to(selected)
    env_file = tmp_path / "server.env"
    env_file.write_text('COMPRESSOR_DEVICE="cuda"\n', encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "EXPERIMENT_ENV_FILE": str(env_file),
            "COMPRESSION_PYTHON": str(selected),
            "GPU_COMPRESSOR_SMOKE_TEST_APPROVED": "true",
        }
    )
    command = (
        f"source {str(SCRIPTS / 'lib.sh')!r}; "
        "require_compressor_device_for_conditions cuda longllmlingua_2x"
    )

    environment["GPU_COMPRESSOR_APPROVED_PYTHON"] = str(other)
    mismatch = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert mismatch.returncode == 2
    assert "does not match the approved identity" in mismatch.stderr

    environment["GPU_COMPRESSOR_APPROVED_PYTHON"] = str(alias)
    matched = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert matched.returncode == 0, matched.stderr


def test_jupyter_config_consumes_only_argon2_password_verifier(
    monkeypatch,
    tmp_path: Path,
):
    verifier = "argon2:$argon2id$v=19$m=10240,t=10,p=8$c2FsdA$dmVyaWZpZXI"
    hash_path = tmp_path / "password_hash"
    hash_path.write_text(verifier + "\n", encoding="utf-8")
    hash_path.chmod(0o600)
    monkeypatch.setenv("JUPYTER_PASSWORD_HASH_FILE", str(hash_path))
    monkeypatch.setenv("JUPYTER_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("JUPYTER_PORT", "8888")
    config = SimpleNamespace(
        ServerApp=SimpleNamespace(),
        IdentityProvider=SimpleNamespace(),
        PasswordIdentityProvider=SimpleNamespace(),
    )

    runpy.run_path(
        str(ROOT / "web_control" / "jupyter_server_config.py"),
        init_globals={"c": config},
    )

    assert config.IdentityProvider.token == ""
    assert config.PasswordIdentityProvider.hashed_password == verifier
    assert config.ServerApp.ip == "127.0.0.1"
    assert config.ServerApp.port == 8888


def test_jupyter_service_lifecycle_is_ownership_scoped():
    script = _script("20_setup_jupyter_web.sh")
    assert "load_owned_state" in script
    assert "commu-jupyter-install-v2" in script
    assert "ownership_nonce" in script
    assert "venv_marker_sha256" in script
    assert "jupyter_sha256" in script
    assert "password_hash_sha256" in script
    assert "require_service_fragment" in script
    assert "Refusing to overwrite unowned Jupyter artifact" in script
    assert 'systemctl --user disable --now "${SERVICE_NAME}"' in script
    assert "rollback_install" in script
    assert "restore | uninstall" in script
    assert "service_is_active || service_is_enabled" in script


def test_network_condition_apply_is_transactional_and_reset_is_identity_scoped():
    script = _script("11_network_condition.sh")

    assert "owner=commu-network-condition-v1" in script
    assert "flock -n 9" in script
    assert "trap rollback_partial_apply EXIT" in script
    assert "namespace_identity" in script
    assert "host_ifindex" in script
    assert "host_alias" in script
    assert 'ip link set dev "${HOST_IF}" alias "${OWNER_ALIAS}"' in script
    assert "refusing name-only cleanup" in script

    state_refusal = script.index(
        'if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]'
    )
    state_armed = script.index("write_state applying", state_refusal)
    namespace_create = script.index('ip netns add "${NETNS}"', state_armed)
    assert state_refusal < state_armed < namespace_create

    reset_case = script.index("  reset)")
    state_load = script.index("load_owned_state", reset_case)
    verified_cleanup = script.index("cleanup_owned_resources", state_load)
    state_remove = script.index('rm -f "${STATE_FILE}"', verified_cleanup)
    assert reset_case < state_load < verified_cleanup < state_remove


def test_outer_network_ownership_is_claimed_only_after_successful_apply():
    patterns = {
        "12_run_preliminary_resumable.sh": (
            '"${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"'
        ),
        "18_run_lab_matrix.sh": (
            '"${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"'
        ),
        "19_run_lab_sessions.sh": (
            '"${SCRIPT_DIR}/11_network_condition.sh" apply "${network}"'
        ),
        "22_validate_protocol_pilots.sh": (
            'bash "${SCRIPT_DIR}/11_network_condition.sh" apply baseline'
        ),
    }
    for name, apply_pattern in patterns.items():
        script = _script(name)
        apply_index = script.index(apply_pattern)
        claim_index = script.index("NETWORK_OWNED=1", apply_index)
        state_index = script.index("write_lifecycle_state", claim_index)
        assert apply_index < claim_index < state_index


def test_calibration_cleanup_signals_only_the_recorded_iperf_listener():
    script = _script("15_calibrate_link.sh")

    assert "IPERF_START_TICKS" in script
    assert "IPERF_PORT" in script
    assert "iperf_pid_matches" in script
    assert 'readlink -f "/proc/${pid}/exe"' in script
    assert 'mapfile -d \'\' -t argv <"/proc/${pid}/cmdline"' in script
    assert "trap cleanup EXIT" in script
    term_index = script.index('kill -TERM "${IPERF_PID}"')
    match_index = script.rfind("iperf_pid_matches", 0, term_index)
    assert match_index < term_index


def test_parallel_launchers_cleanup_exact_registered_children_on_exit():
    for name in (
        "02_prepare_manifest_parallel.sh",
        "05_run_profile_parallel.sh",
        "08_run_transport_profile_parallel.sh",
    ):
        script = _script(name)
        assert "pid_start_ticks=()" in script
        assert "cleanup_children_on_exit" in script
        assert "trap cleanup_children_on_exit EXIT" in script
        assert "trap 'exit 130' INT" in script
        assert "trap 'exit 143' TERM" in script
        assert "stop_owned_child" in script
        assert "trap - INT TERM" in script

    library = _script("lib.sh")
    assert "owned_child_pid_matches" in library
    assert "record_owned_session_start_ticks" in library
    assert "owned_session_group_has_live_members" in library
    assert 'kill -"${signal_name}" -- "-${pid}"' in library
    for name in (
        "02_prepare_manifest_parallel.sh",
        "05_run_profile_parallel.sh",
        "08_run_transport_profile_parallel.sh",
    ):
        script = _script(name)
        assert "require_command setsid" in script
        assert "exec setsid" in script


def test_parallel_aggregation_is_append_only_and_attempt_safe():
    for name in (
        "05_run_profile_parallel.sh",
        "08_run_transport_profile_parallel.sh",
    ):
        script = _script(name)
        assert 'output.open("a+"' in script
        assert "fcntl.flock" in script
        assert "attempt_identity" in script
        assert 'row.get("attempt_id")' in script
        assert "trial assigned to multiple workers" in script
        assert "os.fsync" in script
        assert "output.write_text" not in script
        assert "latest = {}" not in script

    pipeline = _script("10_run_local_transport_pipeline.sh")
    assert "logical trials" in pipeline
    assert "complete captured attempt" in pipeline
    assert "Refusing legacy bare PID file" in pipeline
    assert "launcher_pid_matches" in pipeline
    assert "process_start_ticks" in pipeline


def test_secure_parallel_launcher_uses_frozen_dynamic_worker_topology():
    launcher = _script("08_run_transport_profile_parallel.sh")
    matrix = _script("18_run_lab_matrix.sh")
    helper = _script("worker_topology.sh")

    assert 'source "${SCRIPT_DIR}/worker_topology.sh"' in launcher
    assert 'for ((worker=0; worker<PARALLEL_WORKERS; worker++))' in launcher
    assert "for worker in 0 1" not in launcher
    assert '--worker-count "${PARALLEL_WORKERS}"' in launcher
    assert '--worker-gpu-index "${WORKER_GPU_INDEXES[worker]}"' in launcher
    assert '--worker-gpu-uuid "${WORKER_GPU_UUIDS[worker]}"' in launcher
    assert "for worker in range(worker_count)" in launcher
    assert "worker GPU UUID does not match topology" in launcher
    assert "backend port does not match topology" in launcher
    assert launcher.index('ensure_worker_topology "${OUTPUT_ROOT}"') < launcher.index(
        'mkdir -p "${RUN_DIR}"'
    )

    assert 'load_measured_worker_topology()' in helper
    assert 'declare -F load_worker_topology' in helper
    assert 'selectors=("${WORKER_GPU_IDS[@]}")' in helper
    assert 'discovered_uuids=("${WORKER_GPU_UUIDS[@]}")' in helper
    assert 'tls_ports=("${EXPECTED_PROXY_TCP_PORTS[@]}")' in helper
    assert 'http3_ports=("${EXPECTED_PROXY_UDP_PORTS[@]}")' in helper
    assert 'PARALLEL_WORKERS must be 1 or 2' in helper
    assert "--query-gpu=index,uuid" in helper
    assert '--model "${VLLM_MODEL}"' in helper
    assert '--served-model-name "${VLLM_SERVED_MODEL_NAME}"' in helper
    assert '--model-revision "${VLLM_MODEL_REVISION:-}"' in helper
    assert matrix.index('ensure_worker_topology "${RUNS_ROOT_ABS}"') < matrix.index(
        'mkdir -p "${LIFECYCLE_DIR}"'
    )


def test_network_partial_cleanup_never_bypasses_recorded_identity():
    script = _script("11_network_condition.sh")
    rollback = script.index("rollback_partial_apply()")
    rollback_cleanup = script.index("cleanup_owned_resources", rollback)
    assert "cleanup_owned_resources 1" not in script
    assert "trust_current_apply" not in script
    assert "DEFER_SIGNALS=1" in script
    assert "finish_deferred_signals" in script
    assert rollback < rollback_cleanup
    assert '[[ "$(namespace_identity)" != "${NAMESPACE_ID}" ]]' in script
    assert '[[ "$(host_ifindex)" != "${HOST_IFINDEX}"' in script
    assert '[[ -L "${STATE_FILE}" || -L "${LOCK_FILE}" ]]' in script


def test_preliminary_caddy_rechecks_identity_before_kill_escalation():
    script = _script("12_run_preliminary_resumable.sh")
    term_index = script.index('kill -TERM "${CADDY_PID}"')
    kill_index = script.index('kill -KILL "${CADDY_PID}"', term_index)
    identity_index = script.rfind("caddy_pid_matches", term_index, kill_index)
    assert term_index < identity_index < kill_index
    assert "Caddy identity changed during shutdown" in script
