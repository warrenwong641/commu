from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "24_switch_vllm_topology.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _bash() -> str:
    bash = shutil.which("bash")
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        bash = str(candidate) if candidate.is_file() else ""
    if not bash:
        pytest.skip("bash is unavailable")
    return bash


def _bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _config(tmp_path: Path, workers: int, gpus: str, extra: str = "") -> Path:
    path = tmp_path / "target.env"
    path.write_text(
        "\n".join(
            (
                f'PARALLEL_WORKERS="{workers}"',
                f'CUDA_VISIBLE_DEVICES="{gpus}"',
                'VLLM_HOST="127.0.0.1"',
                'VLLM_PORT="8000"',
                'VLLM_PORT_STEP="1"',
                'VLLM_MODEL="Qwen/Qwen3.5-9B"',
                'VLLM_MODEL_REVISION="revision"',
                'RUNS_ROOT="runs/topology-test"',
                extra,
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def _validate(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), _bash_path(SCRIPT), "validate-config", "--target-env", _bash_path(path)],
        cwd=path.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    ("workers", "gpus", "ports"),
    ((1, "2", "ports=8000"), (2, "2,1", "ports=8000,8001")),
)
def test_validate_config_accepts_exact_one_and_two_worker_topologies(
    tmp_path: Path, workers: int, gpus: str, ports: str
):
    result = _validate(_config(tmp_path, workers, gpus))
    assert result.returncode == 0, result.stderr
    assert f"workers={workers}" in result.stdout
    assert f"gpu_indices={gpus}" in result.stdout
    assert ports in result.stdout


@pytest.mark.parametrize(
    ("workers", "gpus", "message"),
    (
        (3, "2,1,0", "must be 1 or 2"),
        (1, "2,1", "exactly one GPU per worker"),
        (2, "2,2", "two distinct GPU indices"),
        (2, "2, 1", "must not contain whitespace"),
    ),
)
def test_validate_config_rejects_ambiguous_topologies(
    tmp_path: Path, workers: int, gpus: str, message: str
):
    result = _validate(_config(tmp_path, workers, gpus))
    assert result.returncode == 2
    assert message in result.stderr


def test_validate_config_rejects_persisted_credentials(tmp_path: Path):
    result = _validate(_config(tmp_path, 1, "2", 'LOCAL_VLLM_API_KEY="forbidden"'))
    assert result.returncode == 2
    assert "credential assignment" in result.stderr
    assert "forbidden" not in result.stdout + result.stderr


def test_switcher_is_ownership_scoped_and_does_not_persist_or_expose_key():
    source = _source()
    state_writer = source[source.index("write_state() {") : source.index('if [[ "${ACTION}" == "validate-config"')]
    start_body = source[source.index("start_service() {") : source.index("wait_service_ready() {")]

    assert 'kill -TERM "${pid}"' in source
    assert "pkill" not in source
    assert "kill -KILL" not in source
    assert "kill -- -" not in source
    assert "nvidia-smi" not in source[source.index("stop_verified_controller() {") : source.index("start_service() {")]
    assert 'controller_gpu_processes_exact "${controller}" || return 1' in source
    assert "LOCAL_VLLM_API_KEY" not in state_writer
    assert 'export LOCAL_VLLM_API_KEY="${api_key}"' in start_body
    assert "--header @-" in source
    assert "Authorization: Bearer ${" not in source
    assert "/v1/models" in source


def test_switcher_has_fail_closed_identity_checks_and_automatic_rollback():
    source = _source()
    stop_body = source[source.index("stop_verified_controller() {") : source.index("STARTED_PID=")]

    assert 'validate_controller "${pid}" "${ticks}" "${config}" "${uid}" || return 1' in stop_body
    assert 'wait_controller_exit "${pid}" "${ticks}" || return 1' in stop_body
    assert 'for port in 8000 8001; do port_closed "${port}" || return 1; done' in stop_body
    assert "attempting verified rollback" in source
    assert 'start_service "${PREVIOUS_CONFIG}"' in source
    assert 'write_state "${PREVIOUS_CONFIG}" rolled_back' in source
    assert 'if [[ "${CFG_WORKERS}" == "1" ]]; then port_closed 8001 || return 1; fi' in source
