from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
LIB = ROOT / "scripts" / "lib.sh"


def _bash() -> str:
    bash = shutil.which("bash")
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
            "Git/bin/bash.exe"
        )
        bash = str(candidate) if candidate.is_file() else None
    if bash is None:
        pytest.skip("bash is unavailable")
    return bash


def _bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _resolve(tmp_path: Path, workers: str, gpus: str, port: str = "8000"):
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "\n".join(
            (
                f'PARALLEL_WORKERS="{workers}"',
                f'CUDA_VISIBLE_DEVICES="{gpus}"',
                f'VLLM_PORT="{port}"',
                'VLLM_PORT_STEP="1"',
                "",
            )
        ),
        encoding="utf-8",
    )
    command = (
        f'EXPERIMENT_ENV_FILE="{_bash_path(env_file)}" '
        f'source "{_bash_path(LIB)}"; '
        "load_worker_topology; "
        "printf '%s|%s|%s\\n' \"${TOPOLOGY_WORKER_COUNT}\" "
        '"${TOPOLOGY_GPU_IDS}" "$(IFS=,; printf \'%s\' "${WORKER_VLLM_PORTS[*]}")"'
    )
    return subprocess.run(
        [_bash(), "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    ("workers", "gpus", "expected"),
    (("1", "2", "1|2|8000"), ("2", "2,1", "2|2,1|8000,8001")),
)
def test_worker_topology_accepts_one_or_two_exact_gpu_mappings(
    tmp_path: Path, workers: str, gpus: str, expected: str
):
    completed = _resolve(tmp_path, workers, gpus)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


@pytest.mark.parametrize(
    ("workers", "gpus", "port"),
    (
        ("3", "2,1,0", "8000"),
        ("2", "2", "8000"),
        ("2", "2,2", "8000"),
        ("1", "GPU-abc", "8000"),
        ("2", "2,1", "65535"),
    ),
)
def test_worker_topology_rejects_ambiguous_or_unsafe_mappings(
    tmp_path: Path, workers: str, gpus: str, port: str
):
    completed = _resolve(tmp_path, workers, gpus, port)
    assert completed.returncode == 2


def test_single_caddy_config_has_only_primary_listener_and_backend():
    single = (ROOT / "configs" / "Caddyfile.single").read_text(encoding="utf-8")
    dual = (ROOT / "configs" / "Caddyfile").read_text(encoding="utf-8")

    assert ":8443" in single and ":8444" in single
    assert ":8543" not in single and ":8544" not in single
    assert "VLLM_SECONDARY_PORT" not in single
    assert ":8543" in dual and ":8544" in dual
