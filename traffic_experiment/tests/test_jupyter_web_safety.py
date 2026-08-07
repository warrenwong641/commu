from __future__ import annotations

import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
WEB_CONTROL = ROOT / "web_control"


def _config_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        ServerApp=SimpleNamespace(),
        IdentityProvider=SimpleNamespace(),
        PasswordIdentityProvider=SimpleNamespace(),
    )


def _run_config(monkeypatch, tmp_path: Path, verifier: str):
    root = tmp_path / "root"
    root.mkdir()
    hash_path = tmp_path / "password_hash"
    hash_path.write_text(verifier + "\n", encoding="utf-8")
    hash_path.chmod(0o600)
    monkeypatch.setenv("JUPYTER_ROOT_DIR", str(root))
    monkeypatch.setenv("JUPYTER_PASSWORD_HASH_FILE", str(hash_path))
    config = _config_namespace()
    runpy.run_path(
        str(WEB_CONTROL / "jupyter_server_config.py"),
        init_globals={"c": config},
    )
    return config


def test_jupyter_config_uses_only_argon2_password_verifier(
    monkeypatch,
    tmp_path: Path,
):
    verifier = "argon2:$argon2id$v=19$m=10240,t=10,p=8$c2FsdA$dmVyaWZpZXI"
    config = _run_config(monkeypatch, tmp_path, verifier)

    assert config.IdentityProvider.token == ""
    assert config.PasswordIdentityProvider.hashed_password == verifier
    assert config.ServerApp.ip == "127.0.0.1"
    assert config.ServerApp.disable_check_xsrf is False


@pytest.mark.parametrize(
    "verifier",
    ["", "plaintext-password", "sha1:legacy", "argon2:not-a-phc-verifier"],
)
def test_jupyter_config_rejects_non_argon2_or_empty_verifier(
    monkeypatch,
    tmp_path: Path,
    verifier: str,
):
    with pytest.raises(RuntimeError, match="Argon2"):
        _run_config(monkeypatch, tmp_path, verifier)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode contract")
def test_jupyter_config_rejects_group_readable_verifier(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "root"
    root.mkdir()
    hash_path = tmp_path / "password_hash"
    hash_path.write_text(
        "argon2:$argon2id$v=19$m=10240,t=10,p=8$c2FsdA$dmVyaWZpZXI\n",
        encoding="utf-8",
    )
    hash_path.chmod(0o640)
    monkeypatch.setenv("JUPYTER_ROOT_DIR", str(root))
    monkeypatch.setenv("JUPYTER_PASSWORD_HASH_FILE", str(hash_path))
    with pytest.raises(RuntimeError, match="mode 0600"):
        runpy.run_path(
            str(WEB_CONTROL / "jupyter_server_config.py"),
            init_globals={"c": _config_namespace()},
        )


def test_jupyter_config_rejects_symlinked_verifier(monkeypatch, tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.write_text(
        "argon2:$argon2id$v=19$m=10240,t=10,p=8$c2FsdA$dmVyaWZpZXI\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    link = tmp_path / "password_hash"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    monkeypatch.setenv("JUPYTER_ROOT_DIR", str(root))
    monkeypatch.setenv("JUPYTER_PASSWORD_HASH_FILE", str(link))
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        runpy.run_path(
            str(WEB_CONTROL / "jupyter_server_config.py"),
            init_globals={"c": _config_namespace()},
        )


def test_jupyter_install_and_checker_never_persist_or_send_bearer_token():
    installer = (SCRIPTS / "20_setup_jupyter_web.sh").read_text(
        encoding="utf-8"
    )
    checker = (SCRIPTS / "21_check_jupyter_web.sh").read_text(encoding="utf-8")
    config = (WEB_CONTROL / "jupyter_server_config.py").read_text(
        encoding="utf-8"
    )
    unit = (WEB_CONTROL / "commu-jupyter.service").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((installer, checker, config, unit))

    for forbidden in (
        "JUPYTER_TOKEN_FILE",
        "Authorization: token",
        "/commu-jupyter/token",
        "openssl rand",
    ):
        assert forbidden not in combined
    assert "JUPYTER_PASSWORD_HASH_FILE" in config
    assert "PasswordIdentityProvider.hashed_password" in config
    assert 'c.IdentityProvider.token = ""' in config
    assert "read -r -s password" in installer
    assert "from jupyter_server.auth import passwd" in installer
    assert "legacy plaintext Jupyter token artifact" in installer
    assert "wait_for_authenticated_listener" in installer
    assert "owned_loopback_listener_present" in installer
    assert '"403"' in installer
    assert "403" in checker
    assert "MainPID" in checker
    assert "curl" in checker
    assert "password_hash" not in checker


def test_jupyter_lifecycle_binds_exact_artifact_and_venv_identity():
    installer = (SCRIPTS / "20_setup_jupyter_web.sh").read_text(
        encoding="utf-8"
    )

    for state_field in (
        "schema=commu-jupyter-install-v2",
        "config_sha256=",
        "environment_sha256=",
        "password_hash_sha256=",
        "unit_sha256=",
        "venv_marker_sha256=",
        "jupyter_sha256=",
        "ownership_nonce=",
    ):
        assert state_field in installer
    assert 'require_regular_owned_file "${CONFIG_FILE}" 600' in installer
    assert 'require_regular_owned_file "${UNIT_FILE}" 600' in installer
    assert 'require_service_fragment' in installer
    assert 'validate_owned_venv_path "${JUPYTER_VENV}"' in installer
    assert 'Refusing to adopt an existing JUPYTER_VENV' in installer
    assert "load_owned_state strict" in installer
    assert "prepare_owned_action cleanup" in installer
