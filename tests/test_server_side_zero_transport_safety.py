from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1] / "server_side_zero_transport"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_gateway_cleanup_verifies_exact_process_identity_and_listener_closure():
    script = _read("run.sh")
    assert "gateway_pid_matches" in script
    assert "gateway_start_ticks" in script
    assert 'readlink -f "/proc/$pid/exe"' in script
    assert 'gateway_listener_closed' in script
    assert 'kill -KILL "$gateway_pid"' in script
    assert '"--upstream"' in script
    assert '"$VLLM_URL"' in script
    assert "upstream=$VLLM_URL" in script
    assert script.index(
        'gateway_pid_matches "$gateway_pid" "$gateway_start_ticks"'
    ) < script.index('kill -KILL "$gateway_pid"')


def test_server_side_configs_reject_credential_assignments_before_source():
    credential_names = (
        "LOCAL_VLLM_API_KEY",
        "VLLM_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    )
    for name in ("run.sh", "verify_existing_vllm.sh"):
        script = _read(name)
        check = script.index("credential assignment found")
        source = script.index('source "$CONFIG"')
        assert check < source
        for credential_name in credential_names:
            assert credential_name in script
    example = _read("config.env.example")
    executable = [
        line.strip()
        for line in example.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for credential_name in credential_names:
        assert not any(
            line.startswith(f"{credential_name}=") for line in executable
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode semantics required")
def test_server_side_launcher_refuses_symlinked_runs_directory(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")

    root = tmp_path / "server_side_zero_transport"
    root.mkdir()
    shutil.copy2(ROOT / "run.sh", root / "run.sh")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "runs").symlink_to(outside, target_is_directory=True)
    config = root / "config.env"
    config.write_text(
        "\n".join(
            (
                "GATEWAY_HOST=127.0.0.1",
                "GATEWAY_PORT=18080",
                "MODEL_NAME=test-model",
                "PROMPT=test",
                "MAX_TOKENS=1",
                "VLLM_URL=http://127.0.0.1:18000",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["VLLM_API_KEY"] = "process-only-test-value"
    result = subprocess.run(
        [bash, str(root / "run.sh"), str(config)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "symlinked runs path" in result.stderr
    assert not list(outside.iterdir())
