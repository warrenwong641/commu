from __future__ import annotations

import os
import shutil
import subprocess
import json
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


from traffic_experiment.traffic_measure.cli import _parser
from traffic_experiment.traffic_measure.worker_topology import (
    MARKER_NAME,
    ensure_worker_topology,
)


def _topology(worker_count: int = 2) -> dict:
    workers = [
        {
            "worker_index": 0,
            "gpu_selector": "0",
            "gpu_index": 0,
            "gpu_uuid": "GPU-0000",
            "vllm_port": 8000,
            "secure_ports": {"tls13": 8443, "http3": 8444},
        },
        {
            "worker_index": 1,
            "gpu_selector": "1",
            "gpu_index": 1,
            "gpu_uuid": "GPU-1111",
            "vllm_port": 8001,
            "secure_ports": {"tls13": 8543, "http3": 8544},
        },
    ]
    return {
        "schema_version": 1,
        "worker_count": worker_count,
        "model": "Qwen/Qwen3.5-9B",
        "served_model_name": "Qwen/Qwen3.5-9B",
        "model_revision": "a" * 40,
        "workers": workers[:worker_count],
    }


def test_topology_marker_is_created_and_exact_reuse_is_accepted(tmp_path: Path):
    root = tmp_path / "runs"
    expected = _topology()
    marker = ensure_worker_topology(root, expected)

    assert marker == root / MARKER_NAME
    assert json.loads(marker.read_text(encoding="utf-8")) == expected
    assert ensure_worker_topology(root, expected) == marker


def test_topology_change_in_same_output_tree_is_rejected(tmp_path: Path):
    root = tmp_path / "runs"
    ensure_worker_topology(root, _topology(2))

    with pytest.raises(ValueError, match="worker topology mismatch"):
        ensure_worker_topology(root, _topology(1))


def test_writable_topology_marker_is_rejected(tmp_path: Path):
    root = tmp_path / "runs"
    marker = ensure_worker_topology(root, _topology())
    marker.chmod(0o644)

    with pytest.raises(ValueError, match="topology marker is writable"):
        ensure_worker_topology(root, _topology())


def test_results_without_topology_marker_cannot_be_adopted(tmp_path: Path):
    root = tmp_path / "runs"
    results = root / "old" / "results.jsonl"
    results.parent.mkdir(parents=True)
    results.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already contains results"):
        ensure_worker_topology(root, _topology())
    assert not (root / MARKER_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_symlinked_output_tree_and_marker_are_rejected(tmp_path: Path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="contains a symlink"):
        ensure_worker_topology(alias, _topology())

    marker = actual / MARKER_NAME
    marker.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="not a regular file"):
        ensure_worker_topology(actual, _topology())


def test_run_cli_accepts_physical_worker_identity():
    args = _parser().parse_args(
        [
            "run",
            "--manifest",
            "manifest.jsonl",
            "--output-dir",
            "run",
            "--samples",
            "1",
            "--repetitions",
            "1",
            "--worker-count",
            "2",
            "--worker-index",
            "1",
            "--worker-gpu-index",
            "7",
            "--worker-gpu-uuid",
            "GPU-physical",
        ]
    )

    assert args.worker_gpu_index == 7
    assert args.worker_gpu_uuid == "GPU-physical"
