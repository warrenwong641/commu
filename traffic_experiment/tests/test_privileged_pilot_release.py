from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "traffic_experiment"
VALIDATOR = EXPERIMENT / "scripts" / "privileged_pilot_config.py"
BUILDER = EXPERIMENT / "scripts" / "26_create_privileged_pilot_bundle.sh"
INSTALLER = EXPERIMENT / "scripts" / "27_install_privileged_pilot_release.sh"
RUNNER = EXPERIMENT / "scripts" / "28_run_privileged_protocol_pilots.sh"
PILOT = EXPERIMENT / "scripts" / "22_validate_protocol_pilots.sh"
EXAMPLE = EXPERIMENT / "privileged-pilot.env.example"
PILOT_LOCK = EXPERIMENT / "requirements-privileged-pilot.lock"
WHEEL_MANIFEST = EXPERIMENT / "privileged-pilot-wheels.cp312-linux-x86_64.sha256"


def _valid_source() -> str:
    return (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("VLLM_MODEL_REVISION=\"\"", f'VLLM_MODEL_REVISION="{"a" * 40}"')
        .replace("MANIFEST_SHA256=\"\"", f'MANIFEST_SHA256="{"b" * 64}"')
        .replace(
            "SUMMARY_MANIFEST_SHA256=\"\"",
            f'SUMMARY_MANIFEST_SHA256="{"c" * 64}"',
        )
    )


def _run_validator(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_privileged_config_normalizes_exact_commit_and_validates(tmp_path: Path) -> None:
    repository_sha = "d" * 40
    source = tmp_path / "input.env"
    output = tmp_path / "output.env"
    source.write_text(_valid_source(), encoding="utf-8")

    result = _run_validator(
        "normalize",
        "--input",
        str(source),
        "--output",
        str(output),
        "--repository-sha",
        repository_sha,
    )
    assert result.returncode == 0, result.stderr
    normalized = output.read_text(encoding="utf-8")
    assert "@REPOSITORY_SHA@" not in normalized
    assert f"/var/lib/commu-protocol-pilots/{repository_sha}/@GPU_SCOPE@/runs" in normalized
    assert 'CUDA_VISIBLE_DEVICES="@GPU_INDEX@"' in normalized

    checked = _run_validator(
        "check",
        "--input",
        str(output),
        "--repository-sha",
        repository_sha,
    )
    assert checked.returncode == 0, checked.stderr

    runtime = tmp_path / "runtime.env"
    materialized = _run_validator(
        "materialize",
        "--input",
        str(output),
        "--output",
        str(runtime),
        "--repository-sha",
        repository_sha,
        "--gpu-index",
        "4",
        "--gpu-uuid",
        "GPU-11111111-2222-3333-4444-555555555555",
    )
    assert materialized.returncode == 0, materialized.stderr
    runtime_text = runtime.read_text(encoding="utf-8")
    assert 'CUDA_VISIBLE_DEVICES="4"' in runtime_text
    assert (
        f"/var/lib/commu-protocol-pilots/{repository_sha}/"
        "gpu-4-GPU-11111111-2222-3333-4444-555555555555/runs"
    ) in runtime_text


def test_privileged_config_rejects_invalid_runtime_gpu_identity(tmp_path: Path) -> None:
    repository_sha = "d" * 40
    source = tmp_path / "input.env"
    normalized = tmp_path / "normalized.env"
    source.write_text(_valid_source(), encoding="utf-8")
    result = _run_validator(
        "normalize",
        "--input",
        str(source),
        "--output",
        str(normalized),
        "--repository-sha",
        repository_sha,
    )
    assert result.returncode == 0, result.stderr

    for label, index, uuid in (
        ("leading-zero index", "04", "GPU-11111111-2222-3333-4444-555555555555"),
        ("negative index", "-1", "GPU-11111111-2222-3333-4444-555555555555"),
        ("unsafe UUID", "4", "GPU-../../root"),
    ):
        runtime = tmp_path / f"{label.replace(' ', '-')}.env"
        materialized = _run_validator(
            "materialize",
            "--input",
            str(normalized),
            "--output",
            str(runtime),
            "--repository-sha",
            repository_sha,
            "--gpu-index",
            index,
            "--gpu-uuid",
            uuid,
        )
        assert materialized.returncode != 0, label


def test_privileged_config_rejects_shell_secrets_and_unsafe_overrides(tmp_path: Path) -> None:
    repository_sha = "d" * 40
    mutations = {
        "shell syntax": ('PROFILE="main"', 'PROFILE="$(id)"'),
        "credential": (
            'PROFILE="main"',
            'PROFILE="main"\nLOCAL_VLLM_API_KEY="secret"',
        ),
        "commented credential": (
            'PROFILE="main"',
            'PROFILE="main"\n# HF_TOKEN="secret"',
        ),
        "external provider": (
            'OPENROUTER_MODEL=""',
            'OPENROUTER_MODEL="provider/model"',
        ),
        "dual worker": ('PARALLEL_WORKERS="1"', 'PARALLEL_WORKERS="2"'),
        "fixed gpu": (
            'CUDA_VISIBLE_DEVICES="@GPU_INDEX@"',
            'CUDA_VISIBLE_DEVICES="2"',
        ),
        "external output": (
            'RUNS_ROOT="/var/lib/commu-protocol-pilots/@REPOSITORY_SHA@/@GPU_SCOPE@/runs"',
            'RUNS_ROOT="/home/user/runs"',
        ),
        "host cidr": ('HOST_VETH_CIDR="10.200.0.1/24"', 'HOST_VETH_CIDR="10.9.0.1/24"'),
        "client cidr": ('CLIENT_VETH_CIDR="10.200.0.2/24"', 'CLIENT_VETH_CIDR="10.9.0.2/24"'),
        "proxy host": ('SECURE_PROXY_HOST="10.200.0.1"', 'SECURE_PROXY_HOST="10.9.0.1"'),
        "runtime override": ('PROFILE="main"', 'PROFILE="main"\nRUNNER_PYTHON="/tmp/python"'),
        "loader override": ('PROFILE="main"', 'PROFILE="main"\nexport LD_LIBRARY_PATH="/tmp"'),
    }
    for label, (before, after) in mutations.items():
        source = tmp_path / f"{label.replace(' ', '-')}.env"
        output = tmp_path / f"{label.replace(' ', '-')}.out.env"
        source.write_text(_valid_source().replace(before, after), encoding="utf-8")
        result = _run_validator(
            "normalize",
            "--input",
            str(source),
            "--output",
            str(output),
            "--repository-sha",
            repository_sha,
        )
        assert result.returncode != 0, label
        assert "secret" not in result.stdout + result.stderr


def test_release_builder_is_credential_free_and_binds_runtime_and_caddy() -> None:
    text = BUILDER.read_text(encoding="utf-8")
    assert '[[ "${EUID}" -ne 0 ]]' in text
    assert "git -c core.autocrlf=false -c core.eol=lf" in text
    assert '-C "${REPOSITORY_ROOT}" archive' in text
    assert "xargs -0 sha256sum --text --" in text
    assert 'cp -aL -- "${RUNNER_WHEELHOUSE}"' in text
    assert "wheelhouse filenames/coverage differ" in text
    assert 'cp -- "${CADDY_BIN}"' in text
    assert "--hard-dereference" in text
    assert "RELEASE_FILES.sha256" in text
    assert "LOCAL_VLLM_API_KEY" not in text
    assert "18_run_lab_matrix" not in text
    assert "service_state_root=" in text
    assert "expected_gpu_uuid=" not in text


def test_review_archive_is_independent_of_ambient_git_line_endings() -> None:
    def archive_digest(autocrlf: str, eol: str) -> bytes:
        archive = subprocess.check_output(
            [
                "git",
                "-c",
                f"core.autocrlf={autocrlf}",
                "-c",
                f"core.eol={eol}",
                # These are the builder's final, authoritative overrides.
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "-C",
                str(ROOT),
                "archive",
                "--format=tar",
                "HEAD",
            ]
        )
        return hashlib.sha256(archive).digest()

    assert archive_digest("true", "crlf") == archive_digest("false", "lf")


def test_installer_pins_bundle_rejects_links_and_hardens_before_execution() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'exec 9<"${ARCHIVE}"' in text
    assert '/usr/bin/cat <&9 >"${COPIED_ARCHIVE}"' in text
    assert '[[ "${ACTUAL_ARCHIVE_SHA}" == "${EXPECTED_ARCHIVE_SHA}" ]]' in text
    assert "EXPECTED_REVIEWED_CODE_MANIFEST_SHA256=" in text
    assert "bundle policy is outside this installer's authorized service scope" in text
    assert "EXPECTED_SERVICE_STATE_ROOT=" in text
    assert "EXPECTED_GPU_INDEX=" not in text
    assert "EXPECTED_GPU_UUID=" not in text
    assert "--only-binary=:all:" in text
    assert "requirements-privileged-pilot.lock" in text
    assert "--copies" in text
    assert "member.isdir() or member.isreg()" in text
    assert 'path.parts[0] != "release"' in text
    assert "release manifest coverage mismatch" in text
    assert text.index("release manifest coverage mismatch") < text.index(
        "/usr/bin/sha256sum --check --strict --quiet RELEASE_FILES.sha256"
    )
    assert '/usr/bin/find "${RELEASE}" -type f -exec chmod 0444 {} +' in text
    assert text.index('chmod 0555 "${CADDY}"') < text.index(
        '"${CADDY}" version'
    )
    assert "/opt/commu-protocol-pilots/releases" in text
    assert "/var/lib/commu-protocol-pilots" in text
    assert "LOCAL_VLLM_API_KEY" not in text
    assert "18_run_lab_matrix" not in text


def test_root_runner_has_clean_environment_lock_and_fail_closed_inventory() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert "/usr/bin/env -i" in text
    assert "PATH=\"${FIXED_PATH}\"" in text
    assert "PYTHONNOUSERSITE=1" in text
    assert 'SERVICE_LOCK_DIR="${SERVICE_STATE}.lock.d"' in text
    assert 'GLOBAL_LOCK_FILE="/run/lock/commu-protocol-pilots/vllm-topology-${SERVICE_UID}.lock"' in text
    assert text.index("acquire_service_lock") < text.index(
        'PROTOCOL_VALIDATION_ROOT="${PROTOCOL_ROOT}"'
    )
    assert "verify_admission" in text
    assert "ss_rows" in text
    assert 'links="$(/usr/sbin/ip -o link show 2>/dev/null)" || die' in text
    assert 'netns="$(/usr/sbin/ip netns list 2>/dev/null)" || die' in text
    assert 'LOCAL_VLLM_API_KEY="${API_KEY}"' in text
    assert "recover_api_key" in text
    assert "printf 'Authorization: Bearer %s\\n'" in text
    assert "18_run_lab_matrix" not in text
    assert "19_run_lab_sessions" not in text
    assert 'source "/home/wongshingyin/commu' not in text
    assert '"${HOME}" == /root' in text
    assert '"${LANG}" == C.UTF-8' in text
    assert '"${PYTHONSAFEPATH}" == 1' in text
    assert "unsanitized environment variable" in text
    scan = text.index("while IFS='=' read -r inherited_name _; do")
    clean = text.index("cd /\n# Bash exports OLDPWD")
    fixed = text.index('[[ "${HOME}" == /root')
    assert scan < clean < fixed
    assert "|OLDPWD|" not in text
    assert "DEFER_PROTOCOL_ADMISSION_PUBLICATION=true" in text
    assert "publish_deferred_admission" in text
    assert "snapshot_user_file" in text
    assert "O_NOFOLLOW" in text
    assert "EXPECTED_API_VLLM" in text
    assert "process_exe" in text
    assert '"$(process_exe "${controller}")" == "${EXPECTED_CONTROLLER_EXE}"' in text
    assert '"$(process_exe "${engine}")" == "$(/usr/bin/readlink -e -- "${EXPECTED_API_PYTHON}")"' in text
    assert "select_gpu_output_hierarchy" in text
    assert "--service-state /absolute/path/to/service.state" in text
    assert "EXPECTED_GPU_UUID_PIN" not in text
    assert 'while [[ "${engine_title}" == *" " ]]' in text
    assert '"${engine_title}" == \'VLLM::EngineCore\'' in text
    assert '"${engine_args[0]}" == \'VLLM::EngineCore\'' not in text
    publish = text.index("publish_deferred_admission\n")
    assert text.rfind("verify_active_service\n", 0, publish) != -1
    assert text.index("verify_active_service\n", publish) != -1


def test_pilot_entrypoint_no_longer_advertises_matrix_and_ip_inventory_fails_closed() -> None:
    text = PILOT.read_text(encoding="utf-8")
    assert "inventory_network_resources" in text
    assert 'links="$(ip -o link show 2>/dev/null)"' in text
    assert 'netns="$(ip netns list 2>/dev/null)"' in text
    assert "18_run_lab_matrix" not in text
    assert "--preserve-env=PATH,LOCAL_VLLM_API_KEY" not in text


def test_example_has_no_secret_and_output_is_commit_scoped() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "LOCAL_VLLM_API_KEY=" not in text
    assert text.count("@REPOSITORY_SHA@") == 3
    assert 'PARALLEL_WORKERS="1"' in text
    assert 'CUDA_VISIBLE_DEVICES="@GPU_INDEX@"' in text
    assert text.count("@GPU_SCOPE@") == 2
    assert 'VLLM_BIN="/usr/bin/false"' in text
    assert hashlib.sha256(text.encode()).hexdigest()


def test_minimal_pilot_lock_is_hash_pinned_and_wheel_manifest_is_exact() -> None:
    lock = PILOT_LOCK.read_text(encoding="utf-8")
    names = []
    for line in lock.splitlines():
        if "==" in line and not line.lstrip().startswith("#"):
            names.append(line.split("==", 1)[0].lower())
    assert set(names) == {
        "aioquic", "anyio", "attrs", "certifi", "cffi", "cryptography",
        "h11", "httpcore", "httpx", "idna", "pycparser", "pylsqpack",
        "pyopenssl", "pyyaml", "service-identity", "typing-extensions",
    }
    assert lock.count("--hash=sha256:") == len(names)
    wheel_lines = WHEEL_MANIFEST.read_text(encoding="utf-8").splitlines()
    assert len(wheel_lines) == len(names)
    assert all(line.endswith(".whl") and len(line.split("  ", 1)[0]) == 64 for line in wheel_lines)


def test_clean_environment_sentinels_cannot_bypass_exact_value_checks() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    for text, sentinel in (
        (installer, "COMMU_PRIVILEGED_PILOT_INSTALL_CLEAN_ENV"),
        (runner, "COMMU_PRIVILEGED_PILOT_CLEAN_ENV"),
    ):
        assert "/usr/bin/env -i" in text
        assert f"{sentinel}=1" in text
        assert '"${HOME}" == /root' in text
        assert '"${LANG}" == C.UTF-8' in text
        assert '"${TZ}" == UTC' in text
        assert "/usr/local/bin" not in text
