from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "29_create_privileged_matrix_bundle.sh"
INSTALLER = SCRIPTS / "30_install_privileged_matrix_release.sh"
SUPERVISOR = SCRIPTS / "31_run_privileged_matrix.sh"
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


def test_installer_requires_pilot_admission_and_existing_shared_lock() -> None:
    text = INSTALLER.read_text()
    assert "EXPECTED_REVIEWED_CODE_MANIFEST_SHA256=" in text
    assert "/opt/commu-secure-matrix/releases" in text
    assert "/var/lib/commu-secure-matrix" in text
    assert (
        'ADMISSION="/var/lib/commu-protocol-pilots/${PILOT_REPOSITORY_SHA}/runs/'
        'protocol_validation/PROTOCOL_VALIDATION_OK"' in text
    )
    assert "root-owned immutable protocol admission is absent" in text
    assert "pilot-installed shared topology lock is absent or unsafe" in text
    assert "EXPECTED_PILOT_REPOSITORY_SHA=7ed49eb0a04c3d4bd69e7361aab31de83426c61f" in text
    assert "LOCK_TMP=" not in text
    assert "31_run_privileged_matrix.sh" in text
    assert 'install -d -o root -g "${SERVICE_GID}" -m 0710' in text


def test_supervisor_holds_locks_for_matrix_and_rechecks_each_cell() -> None:
    text = SUPERVISOR.read_text()
    assert "{check|status|run|resume}" in text
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


def test_request_enters_namespace_then_drops_identity_without_lock_or_key() -> None:
    text = SUPERVISOR.read_text()
    child = text[text.index("/usr/sbin/ip netns exec llm-client") :]
    assert child.index("/usr/sbin/ip netns exec llm-client") < child.index(
        "/usr/bin/setpriv --reuid"
    )
    assert "exec 8>&-" in child
    assert "--clear-groups" in child
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
    if os.name == "nt":
        pytest.skip("POSIX ownership/mode semantics are required")
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
        "--gpu-uuid", "GPU-1234",
        "--service-uid", str(os.getuid()),
        "--model", "Qwen/model",
        "--served-model-name", "Qwen/model",
        "--model-revision", "3" * 40,
    ]
    state.plan_action(parser.parse_args(["create-plan", *values]))
    plan = json.loads((root / "RUN_PLAN.json").read_text())
    assert plan["expected_calls"] == 2808
    assert len(plan["cells"]) == 12
    assert (root / "worker-topology.json").is_file()
    state.plan_action(parser.parse_args(["verify-plan", *values]))
    changed = parser.parse_args(["verify-plan", *values[:-1], "GPU-different"])
    with pytest.raises(ValueError, match="does not match"):
        state.plan_action(changed)


def test_config_example_is_secret_free_and_fixed() -> None:
    text = EXAMPLE.read_text()
    assert "LOCAL_VLLM_API_KEY=" not in text
    assert text.count("@REPOSITORY_SHA@") == 3
    for assignment in (
        'CUDA_VISIBLE_DEVICES="2"',
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
    values["LAB_NETWORKS"] = "baseline realistic"
    with pytest.raises(config.ConfigError, match="LAB_NETWORKS"):
        config.validate_release_config(values, repository_sha, None)


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
