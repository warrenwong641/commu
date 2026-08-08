from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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

    assert qwen_check.returncode == 1, "qwen35 tracked source must not be ignored"
    assert runtime_check.returncode == 0, "runtime environment siblings must be ignored"


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


def test_test_wrapper_rejects_inherited_python_paths(tmp_path: Path) -> None:
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
        ["bash", str(REPOSITORY_ROOT / "scripts" / "run_tests.sh")],
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
        assert "NLTK_DISABLE_IMPORT_SECURITY" not in setup

    assert "requirements.lock" in core_setup
    assert "requirements-runner.lock" in traffic_setup


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
    assert "uv pip install --python .venv-runner/bin/python" in handoff
    assert ".venv-runner/bin/python -P -m pip install" in handoff


def test_locks_contain_hashes_and_pinned_provenance() -> None:
    for relative_lock in (
        "requirements.lock",
        "traffic_experiment/requirements-runner.lock",
    ):
        lock = (REPOSITORY_ROOT / relative_lock).read_text()
        assert "uv 0.11.33" in lock
        assert "CPython 3.11.15" in lock
        assert "x86_64-unknown-linux-gnu" in lock
        assert "--index-url https://pypi.org/simple" in lock
        assert "--hash=sha256:" in lock
