from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "24_switch_vllm_topology.sh"
PROFILE_EXAMPLES = (
    ROOT / "server.vllm-single.env.example",
    ROOT / "server.vllm-dual.env.example",
)


def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def bash() -> str:
    result = shutil.which("bash")
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        result = str(candidate) if candidate.is_file() else ""
    if not result:
        pytest.skip("bash unavailable")
    return result


def bp(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


def config(tmp_path: Path, workers: int, gpus: str, extra: str = "") -> Path:
    path = tmp_path / "target.env"
    path.write_text(
        "\n".join(
            (
                f'PARALLEL_WORKERS="{workers}"', f'CUDA_VISIBLE_DEVICES="{gpus}"',
                'VLLM_HOST="127.0.0.1"', 'VLLM_PORT="8000"', 'VLLM_SECONDARY_PORT="8001"', 'VLLM_PORT_STEP="1"',
                'VLLM_MODEL="Qwen/Qwen3.5-9B"', 'VLLM_SERVED_MODEL_NAME="Qwen/Qwen3.5-9B"',
                'VLLM_MODEL_REVISION="revision"', 'VLLM_BIN="/nonexistent/static-validation"',
                'MAX_MODEL_LEN="65536"', 'GPU_MEMORY_UTILIZATION="0.90"',
                'TENSOR_PARALLEL_SIZE="1"', 'export LD_LIBRARY_PATH="/opt/cuda/lib"',
                'RUNS_ROOT="runs/topology-test"', extra, "",
            )
        ), encoding="utf-8",
    )
    return path


def validate(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash(), bp(SCRIPT), "validate-config", "--target-env", bp(path)],
        cwd=path.parent, capture_output=True, text=True, timeout=10, check=False,
    )


@pytest.mark.parametrize(("workers", "gpus", "ports"), ((1, "2", "ports=8000"), (2, "2,1", "ports=8000,8001")))
def test_accepts_exact_topologies(tmp_path: Path, workers: int, gpus: str, ports: str):
    result = validate(config(tmp_path, workers, gpus))
    assert result.returncode == 0, result.stderr
    assert f"workers={workers}" in result.stdout and f"gpu_indices={gpus}" in result.stdout and ports in result.stdout


@pytest.mark.parametrize("example", PROFILE_EXAMPLES)
def test_switch_profile_examples_are_parser_compatible(example: Path):
    result = validate(example)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("workers", "gpus", "message"), (
    (3, "2,1,0", "must be 1 or 2"),
    (1, "2,1", "exactly one whitespace-free GPU per worker"),
    (2, "2,2", "distinct GPU indices"),
    (2, "2, 1", "exactly one whitespace-free GPU per worker"),
))
def test_rejects_ambiguous_topologies(tmp_path: Path, workers: int, gpus: str, message: str):
    result = validate(config(tmp_path, workers, gpus))
    assert result.returncode == 2 and message in result.stderr


def test_parser_is_inert_and_credentials_are_rejected(tmp_path: Path):
    marker = tmp_path / "executed"
    result = validate(config(tmp_path, 1, "2", f'EVIL="$(touch {marker})"'))
    assert result.returncode == 2 and not marker.exists()
    result = validate(config(tmp_path, 1, "2", 'LOCAL_VLLM_API_KEY="forbidden"'))
    assert result.returncode == 2 and "credential assignment" in result.stderr
    assert "forbidden" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "assignment",
    (
        'LD_PRELOAD="/tmp/hostile.so"',
        'PATH="/tmp/hostile"',
        'BASH_ENV="/tmp/hostile.sh"',
        'PYTHONPATH="/tmp/hostile"',
        'export RUNNER_PYTHON="/tmp/python"',
    ),
)
def test_parser_rejects_unknown_or_unapproved_export_assignments(
    tmp_path: Path, assignment: str
):
    result = validate(config(tmp_path, 1, "2", assignment))
    assert result.returncode == 2
    assert "unknown config assignment" in result.stderr or "only LD_LIBRARY_PATH" in result.stderr


def test_configs_freeze_before_key_recovery_and_are_never_sourced():
    text = source()
    switch = text[text.rindex('[[ -n "${TARGET_ENV}" ]] || die "--target-env required"') :]
    assert switch.index("PREV_FROZEN=") < switch.index('VKEY="$(proc_env_value')
    assert switch.index("TARGET_FROZEN=") < switch.index('VKEY="$(proc_env_value')
    assert 'source "${' not in text
    assert 'LOCAL_VLLM_API_KEY="${key}"' in text
    writer = text[text.index("write_state() {") : text.index("prepare_new_log() {")]
    assert "LOCAL_VLLM_API_KEY" not in writer


def test_stop_is_exact_and_gpu_engines_are_never_signalled():
    text = source()
    stop = text[text.index("stop_controller() {") : text.index("START_PID=")]
    assert 'validate_controller "${pid}" "${ticks}" "${config}" "${uid}" || return 1' in stop
    assert 'capture_owned_resources "${pid}" || return 1' in stop
    assert "old_resources_gone" in stop
    assert 'kill -TERM "${pid}"' in stop
    assert "pkill" not in text and "kill -KILL" not in text and "kill -- -" not in text


def test_ss_target_gpu_and_identity_checks_fail_closed():
    text = source()
    assert 'rows="$(ss_rows -H -ltn "sport = :$1")" || return 1' in text
    assert 'audit_target_gpus || die "target GPU has an unrelated compute process"' in text
    assert '[[ "${allowed}" == true ]] || return 1' in text
    assert 'identity_gone "${VAP[i]}" "${VAT[i]}" || return 1' in text
    assert 'identity_gone "${VEP[i]}" "${VET[i]}" || return 1' in text


def test_target_gpu_is_reaudited_after_stop_immediately_before_start():
    text = source()
    stop_index = text.index('stop_controller "${OLD_C}"')
    empty_index = text.index("if configured_gpus_empty; then", stop_index)
    start_index = text.index('start_service "${TARGET_PATH}"', empty_index)
    assert stop_index < empty_index < start_index
    between = text[empty_index:start_index]
    assert "configured_gpus_empty" in between
    assert "target start refused" in text
    assert 'load_cfg PREV; configured_gpus_empty || die "previous topology GPUs are no longer free; rollback start refused"' in text


def test_unverifiable_captured_controller_must_be_gone_before_fallback_cleanup():
    text = source()
    helper = text[text.index("captured_start_gone() {") : text.index("ss_rows() {")]
    assert 'identity_gone "${pid}" "${ticks}"' in helper
    assert '[[ ! -r "/proc/${pid}/stat" ]] && ! kill -0 "${pid}"' in helper
    assert text.count('captured_start_gone "${START_PID}" "${START_TICKS}" || die') == 2
    target_gate = text.index(
        'captured_start_gone "${START_PID}" "${START_TICKS}" || die "captured target controller identity is still live but unverifiable; rollback refused"'
    )
    target_port_fallback = text.index(
        'for p in 8000 8001; do port_closed "${p}" || die "unowned listener remains; rollback refused"',
        target_gate,
    )
    assert target_gate < target_port_fallback


def test_state_publication_is_durable_and_transactional():
    text = source()
    writer = text[text.index("write_state() {") : text.index("prepare_new_log() {")]
    for expected in ("mktemp", 'sync -f "${tmp}"', 'mv -- "${tmp}" "${STATE_FILE}"', 'sync -f "${STATE_DIR}"'):
        assert expected in writer
    assert 'target_ok}" == true ]] && write_state' in text
    assert "rollback state publication failed" in text
    assert 'stop_controller "${START_PID}"' in text


def test_exact_legacy_v1_is_live_derived_then_upgraded_to_v2():
    text = source()
    legacy_fields = (
        "config_sha256", "controller_pid", "controller_start_ticks", "worker_${i}_port",
        "worker_${i}_gpu_index", "worker_${i}_gpu_uuid", "worker_${i}_api_pid",
        "worker_${i}_engine_pid",
    )
    assert "commu-vllm-service-state-v1" in text
    for field in legacy_fields:
        assert field in text
    assert 'if [[ "${schema}" == commu-vllm-service-state-v2 ]]' in text
    assert "worker_${i}_api_start_ticks" in text and "worker_${i}_engine_start_ticks" in text
    assert "schema=commu-vllm-service-state-v2" in text


def test_files_are_restricted_to_regular_user_owned_targets():
    text = source()
    assert "state must be singly-linked and user-owned" in text
    assert "log must be a new user-owned regular file" in text
    assert '[[ ! -L "${LOCK_DIR}"' in text
    assert "set -o noclobber" in text
