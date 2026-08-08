from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_python_version_and_test_discovery_are_scoped() -> None:
    assert (REPOSITORY_ROOT / ".python-version").read_text().strip() == "3.11"

    pytest_config = (REPOSITORY_ROOT / "pytest.ini").read_text()
    assert "tests\n" in pytest_config
    assert "traffic_experiment/tests" in pytest_config
    assert "traffic_experiment/runs" in pytest_config
    assert "traffic_experiment/environments" in pytest_config


def test_test_wrapper_uses_safe_path_and_safe_working_directory() -> None:
    wrapper = (REPOSITORY_ROOT / "scripts" / "run_tests.sh").read_text()
    assert "mktemp -d" in wrapper
    assert 'cd "${SAFE_WORKDIR}"' in wrapper
    assert 'export PYTHONPATH="${REPOSITORY_ROOT}' in wrapper
    assert "unset ALL_PROXY HTTP_PROXY HTTPS_PROXY" in wrapper
    assert '"${CORE_PYTHON}" -P -m pytest' in wrapper
    assert '"${TRAFFIC_PYTHON}" -P -m pytest' in wrapper
    assert "NLTK_DISABLE_IMPORT_SECURITY" not in wrapper


def test_setup_scripts_install_locked_test_dependencies() -> None:
    core_setup = (REPOSITORY_ROOT / "scripts" / "setup_python.sh").read_text()
    traffic_setup = (
        REPOSITORY_ROOT / "traffic_experiment" / "scripts" / "01_setup_runner.sh"
    ).read_text()

    for setup in (core_setup, traffic_setup):
        assert "uv venv --python 3.11" in setup
        assert "uv pip sync" in setup
        assert "Python 3.11 is required" in setup
        assert "NLTK_DISABLE_IMPORT_SECURITY" not in setup

    assert "requirements.lock" in core_setup
    assert "requirements-runner.lock" in traffic_setup
