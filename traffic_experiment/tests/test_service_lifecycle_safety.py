from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_secure_proxy_stop_checks_all_expected_listeners_before_state_removal():
    script = _script("07_start_secure_proxy.sh")
    stop_body = script[
        script.index("stop_recorded_caddy() {") :
        script.index("cleanup_failed_start() {")
    ]

    assert "EXPECTED_PROXY_TCP_PORTS=(8443 8543)" in script
    assert "EXPECTED_PROXY_UDP_PORTS=(8444 8544)" in script
    assert 'tcp_listeners="$(ss -H -ltn 2>/dev/null)"' in script
    assert 'udp_listeners="$(ss -H -lun 2>/dev/null)"' in script
    assert "if ! command -v ss" in script
    assert "if ! proxy_listener_ports_closed; then" in stop_body

    listener_checks = [
        index
        for index in range(len(stop_body))
        if stop_body.startswith("proxy_listener_ports_closed", index)
    ]
    state_removals = [
        index
        for index in range(len(stop_body))
        if stop_body.startswith('rm -f "${CADDY_STATE_FILE}"', index)
    ]
    assert len(listener_checks) == len(state_removals) == 2
    assert all(check < removal for check, removal in zip(listener_checks, state_removals))
    assert "preserving state" in stop_body
    assert (
        "changed identity or became unverifiable during shutdown; preserving state"
        in stop_body
    )


def test_secure_proxy_refuses_symlinked_state_and_log_targets_before_writes():
    script = _script("07_start_secure_proxy.sh")
    start_body = script[
        script.index("start_proxy() {") : script.index("status_proxy() {")
    ]

    assert '[[ -L "${path}" ]]' in script
    assert 'refuse_unsafe_artifact_target "proxy state" "${CADDY_STATE_FILE}"' in script
    assert 'refuse_unsafe_artifact_target "proxy log" "${CADDY_LOG_FILE}"' in script
    assert start_body.index("prepare_proxy_artifacts") < start_body.index(
        '} >"${CADDY_STATE_FILE}"'
    )
    assert start_body.index("prepare_proxy_artifacts") < start_body.index(
        '>>"${CADDY_LOG_FILE}"'
    )


def test_dual_vllm_cleanup_tracks_exact_active_children_and_checks_ports():
    script = _script("03_start_vllm_dual.sh")
    cleanup_body = script[
        script.index("cleanup_vllm_on_exit() {") :
        script.index("trap cleanup_vllm_on_exit EXIT")
    ]

    for expected in (
        "pid_start_ticks=()",
        "pid_active=()",
        "record_owned_session_start_ticks",
        "stop_owned_child",
        "owned_session_group_has_live_members",
        "exec setsid",
        "wait -n -p exited_pid",
        'tcp_listeners="$(ss -H -ltn 2>/dev/null)"',
    ):
        assert expected in script

    assert cleanup_body.index("stop_owned_child") < cleanup_body.index(
        "vllm_listeners_closed"
    )
    assert 'pid_active[index]=0' in cleanup_body
    assert 'worker_ports+=("$((VLLM_PORT + worker * VLLM_PORT_STEP))")' in script
    assert "pkill" not in script
    assert "nvidia-smi" not in script
    assert 'kill "${pid}"' not in script


def test_dual_vllm_serve_command_is_text_only():
    script = _script("03_start_vllm_dual.sh")
    serve_command = script[
        script.index('exec setsid "${VLLM_BIN}" serve') :
        script.index(') >"${log}" 2>&1 &')
    ]

    assert serve_command.count("--language-model-only") == 1


def _proxy_fixture(tmp_path: Path, tcp_listeners: str, ss_status: int = 0):
    bash = shutil.which("bash")
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
            "Git/bin/bash.exe"
        )
        bash = str(git_bash) if git_bash.is_file() else None
    if bash is None:
        pytest.skip("bash is unavailable")

    def bash_path(path: Path) -> str:
        value = path.resolve().as_posix()
        if os.name == "nt":
            return f"/{value[0].lower()}{value[2:]}"
        return value

    script_dir = tmp_path / "scripts"
    script_dir.mkdir(parents=True)
    proxy = script_dir / "07_start_secure_proxy.sh"
    proxy.write_text(_script("07_start_secure_proxy.sh"), encoding="utf-8")
    (script_dir / "lib.sh").write_text(
        "\n".join(
            (
                "set -euo pipefail",
                f'EXPERIMENT_ROOT="{bash_path(tmp_path)}"',
                'VLLM_HOST="127.0.0.1"',
                'VLLM_PORT="8000"',
                "absolute_from_experiment() {",
                '  if [[ "$1" = /* ]]; then printf \'%s\\n\' "$1";',
                f'  else printf \'%s/%s\\n\' "{bash_path(tmp_path)}" "$1"; fi',
                "}",
                "require_command() { command -v \"$1\" >/dev/null; }",
                "",
            )
        ),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ss = bin_dir / "ss"
    fake_ss.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                '[[ "${FAKE_SS_STATUS:-0}" -eq 0 ]] || exit "${FAKE_SS_STATUS}"',
                'if [[ "$*" == "-H -ltn" ]]; then',
                '  printf \'%s\\n\' "${FAKE_TCP_LISTENERS:-}"',
                'elif [[ "$*" == "-H -lun" ]]; then',
                '  printf \'%s\\n\' "${FAKE_UDP_LISTENERS:-}"',
                "else",
                "  exit 2",
                "fi",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_ss.chmod(0o755)

    state = tmp_path / "secure_proxy.state"
    state.write_text(
        "\n".join(
            (
                "status=running",
                "caddy_pid=99999999",
                "caddy_start_ticks=1",
                f"caddy_config={bash_path(tmp_path)}/Caddyfile",
                "caddy_exe=/nonexistent/caddy",
                "",
            )
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join((str(bin_dir), env.get("PATH", ""))),
            "CADDY_RUN_DIR": bash_path(tmp_path / "run"),
            "CADDY_STATE_FILE": bash_path(state),
            "CADDY_LOG_FILE": bash_path(tmp_path / "secure_proxy.log"),
            "FAKE_TCP_LISTENERS": tcp_listeners,
            "FAKE_UDP_LISTENERS": "",
            "FAKE_SS_STATUS": str(ss_status),
        }
    )
    completed = subprocess.run(
        [bash, bash_path(proxy), "stop"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed, state


def test_secure_proxy_stop_is_fail_closed_in_hermetic_ss_fixture(tmp_path: Path):
    open_result, retained_state = _proxy_fixture(
        tmp_path / "open",
        "LISTEN 0 4096 127.0.0.1:8443 0.0.0.0:*",
    )
    assert open_result.returncode != 0
    assert retained_state.exists()
    assert "preserving state" in open_result.stderr

    failed_result, failed_state = _proxy_fixture(
        tmp_path / "failed",
        "",
        ss_status=1,
    )
    assert failed_result.returncode != 0
    assert failed_state.exists()
    assert "preserving state" in failed_result.stderr

    closed_result, removed_state = _proxy_fixture(tmp_path / "closed", "")
    assert closed_result.returncode == 0
    assert not removed_state.exists()
