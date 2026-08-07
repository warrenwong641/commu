from __future__ import annotations

from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
EXPERIMENT_DIR = SCRIPTS_DIR.parent


def test_parallel_and_remote_clients_do_not_put_api_keys_in_argv():
    paths = [
        SCRIPTS_DIR / "04_check_environment_parallel.sh",
        SCRIPTS_DIR / "05_run_profile_parallel.sh",
        SCRIPTS_DIR / "run_remote_client.ps1",
    ]
    for path in paths:
        executable_lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert "--api-key" not in "\n".join(executable_lines)


def test_remote_client_reads_local_vllm_key_from_environment():
    script = (SCRIPTS_DIR / "run_remote_client.ps1").read_text(encoding="utf-8")
    assert "$env:LOCAL_VLLM_API_KEY" in script
    assert "[string]$ApiKey" not in script
    assert "local-test-key" not in script


def test_cli_accepts_credentials_only_from_process_environment():
    cli = (
        EXPERIMENT_DIR / "traffic_measure" / "cli.py"
    ).read_text(encoding="utf-8")
    assert '"--api-key"' not in cli
    assert "local-test-key" not in cli
    for variable in (
        "LOCAL_VLLM_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
    ):
        assert f'os.environ.get("{variable}", "")' in cli


def test_project_configuration_examples_do_not_store_credential_fields():
    examples = (
        EXPERIMENT_DIR / "configs" / ".env.example",
        EXPERIMENT_DIR / "server.env.example",
        EXPERIMENT_DIR / "server.lab.env.example",
    )
    credential_names = (
        "LOCAL_VLLM_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "HF_TOKEN",
    )
    for path in examples:
        executable_lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for name in credential_names:
            assert not any(line.startswith(f"{name}=") for line in executable_lines)

    library = (SCRIPTS_DIR / "lib.sh").read_text(encoding="utf-8")
    assert "Credential assignment found in ${ENV_FILE}" in library
    for name in credential_names:
        assert name in library


def test_local_measurement_entrypoints_fail_closed_without_process_key():
    for name in (
        "04_check_environment.sh",
        "04_check_environment_parallel.sh",
        "05_run_profile.sh",
        "05_run_profile_parallel.sh",
        "08_run_transport_profile.sh",
        "08_run_transport_profile_parallel.sh",
        "14_run_warm_session.sh",
        "18_run_lab_matrix.sh",
        "19_run_lab_sessions.sh",
        "22_validate_protocol_pilots.sh",
    ):
        script = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        assert "require_value LOCAL_VLLM_API_KEY" in script
