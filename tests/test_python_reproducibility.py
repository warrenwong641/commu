from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _find_working_posix_bash() -> str | None:
    if os.name != "posix":
        return None

    bash = shutil.which("bash")
    if bash is None:
        return None

    try:
        probe = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", ":"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bash if probe.returncode == 0 else None


WORKING_POSIX_BASH = _find_working_posix_bash()


def test_python_version_and_test_discovery_are_scoped() -> None:
    assert (REPOSITORY_ROOT / ".python-version").read_text().strip() == "3.11"

    pytest_config = (REPOSITORY_ROOT / "pytest.ini").read_text()
    assert "tests\n" in pytest_config
    assert "traffic_experiment/tests" in pytest_config
    assert "traffic_experiment/runs" in pytest_config
    assert "traffic_experiment/environments" not in pytest_config
    assert "\n    environments\n" not in pytest_config


def test_qwen35_source_tree_is_trackable_while_runtime_siblings_are_ignored() -> None:
    qwen_source = "traffic_experiment/environments/qwen35/README.md"
    runtime_file = "traffic_experiment/environments/local-runner/bin/python"
    compression_runtime = "traffic_experiment/.venv-compression/bin/python"

    qwen_check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", qwen_source],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    runtime_check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", runtime_file],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    compression_check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", compression_runtime],
        cwd=REPOSITORY_ROOT,
        check=False,
    )

    assert qwen_check.returncode == 1, "qwen35 tracked source must not be ignored"
    assert runtime_check.returncode == 0, "runtime environment siblings must be ignored"
    assert compression_check.returncode == 0, "compression environment must be ignored"


def test_test_wrapper_uses_safe_path_and_safe_working_directory() -> None:
    wrapper = (REPOSITORY_ROOT / "scripts" / "run_tests.sh").read_text()
    assert "mktemp -d" in wrapper
    assert 'cd "${SAFE_WORKDIR}"' in wrapper
    assert 'unset PYTHONHOME' in wrapper
    assert 'export PYTHONPATH="${REPOSITORY_ROOT}"' in wrapper
    assert "PYTHONPATH:+" not in wrapper
    assert "unset ALL_PROXY HTTP_PROXY HTTPS_PROXY" in wrapper
    assert '"${CORE_PYTHON}" -P -m pytest' in wrapper
    assert '"${TRAFFIC_PYTHON}" -P -m pytest' in wrapper
    assert "NLTK_DISABLE_IMPORT_SECURITY" not in wrapper


@pytest.mark.skipif(
    WORKING_POSIX_BASH is None,
    reason="scripts/run_tests.sh requires POSIX and a working Bash executable",
)
def test_test_wrapper_rejects_inherited_python_paths(tmp_path: Path) -> None:
    assert WORKING_POSIX_BASH is not None
    hostile_path = tmp_path / "hostile"
    hostile_path.mkdir()
    (hostile_path / "sentinel.py").write_text(
        "raise RuntimeError('inherited PYTHONPATH was imported')\n", encoding="utf-8"
    )
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[[ \"${PYTHONPATH-}\" == \"${EXPECTED_REPOSITORY_ROOT}\" ]]\n"
        "[[ -z \"${PYTHONHOME+x}\" ]]\n"
        "[[ \":${PYTHONPATH}:\" != *\":${HOSTILE_SENTINEL_PATH}:\"* ]]\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "CORE_PYTHON": str(fake_python),
            "TRAFFIC_PYTHON": str(fake_python),
            "EXPECTED_REPOSITORY_ROOT": str(REPOSITORY_ROOT),
            "HOSTILE_SENTINEL_PATH": str(hostile_path),
            "PYTHONPATH": str(hostile_path),
            "PYTHONHOME": str(hostile_path),
        }
    )

    completed = subprocess.run(
        [WORKING_POSIX_BASH, str(REPOSITORY_ROOT / "scripts" / "run_tests.sh")],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_setup_scripts_install_locked_test_dependencies() -> None:
    core_setup = (REPOSITORY_ROOT / "scripts" / "setup_python.sh").read_text()
    traffic_setup = (
        REPOSITORY_ROOT / "traffic_experiment" / "scripts" / "01_setup_runner.sh"
    ).read_text()

    for setup in (core_setup, traffic_setup):
        assert "uv venv --python 3.11" in setup
        assert "uv pip sync" in setup
        assert "Python 3.11 is required" in setup
        assert "--require-hashes" in setup
        assert "pip install --upgrade" not in setup
        assert "pip install --help" in setup
        assert "NLTK_DISABLE_IMPORT_SECURITY" not in setup

    assert "requirements.lock" in core_setup
    assert "requirements-runner.lock" in traffic_setup
    assert "requirements-compression.lock" in traffic_setup
    assert ".venv-compression" in traffic_setup
    assert 'sync_with_bundled_pip "${RUNNER_VENV}" "${RUNNER_LOCK_FILE}"' in traffic_setup
    assert 'sync_with_bundled_pip "${COMPRESSION_VENV}" "${COMPRESSION_LOCK_FILE}"' in traffic_setup


def test_lock_provenance_and_handoff_cover_supported_paths() -> None:
    compile_script = (
        REPOSITORY_ROOT / "scripts" / "compile_python_locks.sh"
    ).read_text()
    provenance = (
        REPOSITORY_ROOT / "docs" / "python_lock_provenance.md"
    ).read_text()
    handoff = (
        REPOSITORY_ROOT
        / "traffic_experiment"
        / "docs"
        / "local_server_handoff.md"
    ).read_text()

    for expected in (
        'UV_VERSION="0.11.33"',
        'PYTHON_RESOLUTION_VERSION="3.11.15"',
        'TARGET_PLATFORM="x86_64-unknown-linux-gnu"',
        'DEFAULT_INDEX="https://pypi.org/simple"',
        'resolver_identity=',
        "--generate-hashes",
        "--emit-index-url",
    ):
        assert expected in compile_script
    for expected in ("Linux x86_64", "CPython 3.11.15", "uv 0.11.33", "SHA-256"):
        assert expected in provenance
    assert "distinct `.venv-compression`" in handoff
    assert "both paths require the same" in handoff
    assert "SHA-256 hashes" in handoff
    assert "Do not install `requirements-compression.txt`" in handoff
    assert "performs no pip upgrade" in handoff
    assert ".venv-compression/bin/python\" -P" in handoff
    assert 'cd "${TMPDIR:-/tmp}"' in handoff
    assert "env -u PYTHONHOME" in handoff
    assert "CPU-only" in provenance
    assert "torch 2.7.1+cpu" in provenance
    assert "no CUDA toolkit" in provenance
    assert "separately approved compatibility smoke test" in provenance


def test_locks_contain_hashes_and_pinned_provenance() -> None:
    for relative_lock in (
        "requirements.lock",
        "traffic_experiment/requirements-runner.lock",
        "traffic_experiment/requirements-compression.lock",
    ):
        lock = (REPOSITORY_ROOT / relative_lock).read_text()
        assert "uv 0.11.33" in lock
        assert "CPython 3.11.15" in lock
        assert "x86_64-unknown-linux-gnu" in lock
        assert "--index-url https://pypi.org/simple" in lock
        assert "--hash=sha256:" in lock

    compression_lock = (
        REPOSITORY_ROOT / "traffic_experiment" / "requirements-compression.lock"
    ).read_text()
    assert (
        "torch-2.7.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl"
        in compression_lock
    )
    assert "#sha256=a1684793e352f03fa14f78857e55d65d" in compression_lock
    for line in compression_lock.splitlines():
        assert not line.startswith(("cuda-", "nvidia-", "triton=="))


def test_compressor_defaults_and_gpu_gate_are_explicit() -> None:
    for relative_path in (
        "traffic_experiment/server.env.example",
        "traffic_experiment/server.lab.env.example",
    ):
        example = (REPOSITORY_ROOT / relative_path).read_text()
        assert 'COMPRESSOR_DEVICE="cpu"' in example
        assert 'COMPRESSOR_DEVICE="cuda"' not in example

    library = (
        REPOSITORY_ROOT / "traffic_experiment" / "scripts" / "lib.sh"
    ).read_text()
    assert "require_compressor_device_for_conditions()" in library
    assert "GPU_COMPRESSOR_SMOKE_TEST_APPROVED" in library
    assert "default .venv-compression is CPU-only" in library
    source_index = library.index('source "${ENV_FILE}"')
    helper_index = library.index("require_compressor_device_for_conditions()")
    assert source_index < helper_index
    assert "STAGING_ONLY rejection must remain above" in library
    guard_body = library.replace("STAGING_ONLY rejection", "")
    if "STAGING_ONLY" in guard_body:
        assert guard_body.index("STAGING_ONLY") < guard_body.index(
            'source "${ENV_FILE}"'
        )
