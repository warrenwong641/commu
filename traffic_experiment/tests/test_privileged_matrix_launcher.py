from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "privileged_matrix_launch.py"
MEASUREMENT_EXAMPLE = Path(__file__).parents[1] / "privileged-matrix.env.example"


def load_module():
    spec = importlib.util.spec_from_file_location("privileged_matrix_launch_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_plan(path: Path, **changes: object) -> bytes:
    plan = {
        "schema": "commu-secure-single-matrix-plan-v2",
        "repository_sha": "a" * 40,
        "run_id": "main-20260822t1203z",
        "gpu_index": 3,
        "gpu_uuid": "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383",
        "active_config_sha256": "e" * 64,
    }
    plan.update(changes)
    encoded = (json.dumps(plan, sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)
    return encoded


def write_service_template(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'VLLM_HOST="127.0.0.1"',
                'VLLM_PORT="8000"',
                'VLLM_SECONDARY_PORT="8001"',
                'VLLM_MODEL="Qwen/Qwen3.5-9B"',
                'VLLM_SERVED_MODEL_NAME="Qwen/Qwen3.5-9B"',
                f'VLLM_MODEL_REVISION="{"b" * 40}"',
                'CUDA_VISIBLE_DEVICES="7" # portable field',
                'PARALLEL_WORKERS="1"',
                'VLLM_PORT_STEP="1"',
                'TENSOR_PARALLEL_SIZE="1"',
                'MAX_MODEL_LEN="65536"',
                'GPU_MEMORY_UTILIZATION="0.90"',
                'VLLM_BIN="/opt/vllm/bin/vllm"',
                'export LD_LIBRARY_PATH="/opt/vllm/lib"',
                'RUNS_ROOT="/old/source/gpu-7/runs"',
                'CADDY_RUN_DIR="/old/source/gpu-7/caddy"',
                'RANDOM_SEED="42"',
                'MAX_OUTPUT_TOKENS="4096"',
                'SUMMARY_MAX_OUTPUT_TOKENS="4096"',
                'MAIN_REPETITIONS="3"',
                'PROFILE="main"',
                'NETWORK_MTU="1500"',
                'NETWORK_RTT_MS="40"',
                'NETWORK_UPLINK_MBIT="20"',
                'NETWORK_DOWNLINK_MBIT="50"',
                'NETWORK_QUEUE_PACKETS="1000"',
                'LAB_NETWORKS="baseline rtt realistic"',
                'LAB_QA_SAMPLES="32"',
                'LAB_SUMMARY_SAMPLES="20"',
                'LAB_REPETITIONS="3"',
                'LAB_TRANSPORTS="tls13 http3"',
                'LAB_WORKLOADS="qa summary"',
                'CONNECTION_MODE="warm"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_measurement_config(path: Path, **replacements: str) -> None:
    text = MEASUREMENT_EXAMPLE.read_text()
    text = text.replace("@REPOSITORY_SHA@", "a" * 40)
    text = text.replace('VLLM_MODEL_REVISION=""', f'VLLM_MODEL_REVISION="{"b" * 40}"')
    text = text.replace('MANIFEST_SHA256=""', f'MANIFEST_SHA256="{"c" * 64}"')
    text = text.replace(
        'SUMMARY_MANIFEST_SHA256=""', f'SUMMARY_MANIFEST_SHA256="{"d" * 64}"'
    )
    for name, value in replacements.items():
        text = re.sub(
            rf'^{re.escape(name)}="[^"]*"$', f'{name}="{value}"', text,
            flags=re.MULTILINE,
        )
    path.write_text(text)


@pytest.mark.parametrize("value, expected", [("0", 0), ("3", 3), ("107", 107)])
def test_gpu_index_accepts_only_canonical_decimal(value: str, expected: int) -> None:
    launch = load_module()
    assert launch.parse_gpu_index(value) == expected


@pytest.mark.parametrize("value", ["", "00", "03", "+3", "-1", "3 ", " 3", "3.0"])
def test_gpu_index_rejects_ambiguous_values(value: str) -> None:
    launch = load_module()
    with pytest.raises(launch.LaunchConfigError, match="canonical"):
        launch.parse_gpu_index(value)


@pytest.mark.parametrize(
    "value, expected",
    [("20", 20), ("30m", 30), ("1h", 60), ("2h", 120), ("120m", 120)],
)
def test_lease_duration_forms_and_bounds(value: str, expected: int) -> None:
    launch = load_module()
    assert launch.parse_lease_minutes(value) == expected


@pytest.mark.parametrize(
    "value",
    ["0", "15m", "19m", "121", "3h", "030", "02h", "2H", "2.0h", " 30m", "30m "],
)
def test_lease_duration_rejects_noncanonical_or_out_of_policy_values(value: str) -> None:
    launch = load_module()
    with pytest.raises(launch.LaunchConfigError):
        launch.parse_lease_minutes(value)


def test_deadlines_are_derived_from_one_clock_reading() -> None:
    launch = load_module()
    plan = launch.plan_deadlines(1_787_400_000, "90m")
    assert plan.now_epoch == 1_787_400_000
    assert plan.lease_minutes == 90
    assert plan.cleanup_epoch == 1_787_404_800
    assert plan.hard_deadline_epoch == 1_787_405_400


def test_bounded_deadline_caps_requested_lease_at_authorization_cutoff() -> None:
    launch = load_module()
    plan = launch.plan_bounded_deadlines(1_000, "110m", 4_661)
    assert plan.now_epoch == 1_000
    assert plan.lease_minutes == 61
    assert plan.cleanup_epoch == 4_060
    assert plan.hard_deadline_epoch == 4_660
    assert plan.hard_deadline_epoch <= 4_661


def test_bounded_deadline_preserves_shorter_requested_lease() -> None:
    launch = load_module()
    plan = launch.plan_bounded_deadlines(1_000, "30m", 5_000)
    assert plan.lease_minutes == 30
    assert plan.cleanup_epoch == 2_200
    assert plan.hard_deadline_epoch == 2_800


@pytest.mark.parametrize(
    "now, cutoff, match",
    [
        (1_000, 1_000, "later than"),
        (1_000, 999, "later than"),
        (1_000, 2_199, "at least 20"),
        (1_000, 8_201, "no more than two hours"),
    ],
)
def test_bounded_deadline_rejects_invalid_authorization_window(
    now: int, cutoff: int, match: str
) -> None:
    launch = load_module()
    with pytest.raises(launch.LaunchConfigError, match=match):
        launch.plan_bounded_deadlines(now, "110m", cutoff)


def test_bounded_deadline_accepts_exact_policy_boundaries() -> None:
    launch = load_module()
    minimum = launch.plan_bounded_deadlines(1_000, "120m", 2_200)
    maximum = launch.plan_bounded_deadlines(1_000, "120m", 8_200)
    assert minimum.lease_minutes == 20
    assert minimum.hard_deadline_epoch == 2_200
    assert maximum.lease_minutes == 120
    assert maximum.hard_deadline_epoch == 8_200


@pytest.mark.parametrize("value", ["main-1", "main.20260822", "a", "0_safe"])
def test_run_id_accepts_safe_single_path_components(value: str) -> None:
    launch = load_module()
    assert launch.parse_run_id(value) == value


@pytest.mark.parametrize("value", ["", ".hidden", "Main", "a/b", "a b", "a" * 65])
def test_run_id_rejects_unsafe_values(value: str) -> None:
    launch = load_module()
    with pytest.raises(launch.LaunchConfigError, match="safe"):
        launch.parse_run_id(value)


def test_gpu_uuid_requires_exactly_one_canonical_line() -> None:
    launch = load_module()
    uuid = "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383"
    assert launch.parse_gpu_uuid_line(uuid) == uuid
    assert launch.parse_gpu_uuid_line(uuid + "\n") == uuid
    for output in ("", " " + uuid, uuid + " ", uuid + "\n" + uuid, "index, uuid"):
        with pytest.raises(launch.LaunchConfigError, match="exactly one"):
            launch.parse_gpu_uuid_line(output)


def test_service_runtime_root_requires_normalized_absolute_path() -> None:
    launch = load_module()
    value = "/home/service/matrix-segments/attempt-000001"
    assert launch.parse_service_runtime_root(value) == value
    for invalid in (
        "relative/path",
        "/",
        "//server/path",
        "/home/service/",
        "/home/service/../root",
        "/home/service\n/root",
    ):
        with pytest.raises(launch.LaunchConfigError, match="runtime root"):
            launch.parse_service_runtime_root(invalid)


def test_resume_loads_pinned_identity_and_digest(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    encoded = write_plan(plan_path)
    identity = launch.load_resume_identity(
        plan_path,
        asserted_gpu_index="3",
        asserted_gpu_uuid_output="GPU-2783a59c-9574-d4f6-1d4b-bfb505341383\n",
    )
    assert identity.run_id == "main-20260822t1203z"
    assert identity.gpu_index == 3
    assert identity.gpu_uuid == "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383"
    assert identity.active_config_sha256 == "e" * 64
    assert identity.run_plan_sha256 == hashlib.sha256(encoded).hexdigest()
    assert identity.parent_matrix_root is None
    assert identity.parent_repository_sha is None
    assert identity.service_repository_sha == "a" * 40


def test_resume_loads_cross_gpu_continuation_parent_identity(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    parent_sha = "b" * 40
    parent_root = (
        f"/var/lib/commu-secure-matrix/{parent_sha}/"
        "gpu-3-GPU-2783a59c-9574-d4f6-1d4b-bfb505341383/"
        "runs/main-20260822t1203z"
    )
    write_plan(
        plan_path,
        schema="commu-secure-single-matrix-continuation-plan-v1",
        parent_snapshot={
            "run_root": parent_root,
            "repository_sha": parent_sha,
        },
        source_parent_repository_sha=parent_sha,
    )

    identity = launch.load_resume_identity(plan_path)

    assert identity.parent_matrix_root == parent_root
    assert identity.parent_repository_sha == parent_sha
    assert identity.service_repository_sha == parent_sha


def test_resume_rejects_v1_continuation_with_split_parent_and_service_sources(
    tmp_path: Path,
) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    parent_sha = "b" * 40
    parent_root = (
        f"/var/lib/commu-secure-matrix/{parent_sha}/"
        "gpu-3-GPU-2783a59c-9574-d4f6-1d4b-bfb505341383/"
        "runs/main-parent"
    )
    write_plan(
        plan_path,
        schema="commu-secure-single-matrix-continuation-plan-v1",
        parent_snapshot={"run_root": parent_root, "repository_sha": parent_sha},
        source_parent_repository_sha="c" * 40,
    )

    with pytest.raises(launch.LaunchConfigError, match="parent identity is unsafe"):
        launch.load_resume_identity(plan_path)


def test_resume_loads_nested_continuation_run_and_service_identities(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    parent_sha = "b" * 40
    service_sha = "c" * 40
    parent_root = (
        f"/var/lib/commu-secure-matrix/{parent_sha}/"
        "gpu-1-GPU-948f0023-dd20-92a4-3f50-37d369d9bd56/"
        "runs/main-gpu1-cont1"
    )
    write_plan(
        plan_path,
        schema="commu-secure-single-matrix-continuation-plan-v2",
        parent_snapshot={"run_root": parent_root, "repository_sha": parent_sha},
        parent_run_repository_sha=parent_sha,
        source_parent_repository_sha=service_sha,
    )

    identity = launch.load_resume_identity(plan_path)

    assert identity.parent_matrix_root == parent_root
    assert identity.parent_repository_sha == parent_sha
    assert identity.service_repository_sha == service_sha


def test_resume_rejects_noncanonical_continuation_parent_root(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    parent_sha = "b" * 40
    write_plan(
        plan_path,
        schema="commu-secure-single-matrix-continuation-plan-v1",
        parent_snapshot={
            "run_root": f"/var/lib/commu-secure-matrix/{parent_sha}/../escape",
            "repository_sha": parent_sha,
        },
    )
    with pytest.raises(launch.LaunchConfigError, match="parent identity"):
        launch.load_resume_identity(plan_path)


def test_resume_rejects_gpu_override_mismatch(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    write_plan(plan_path)
    with pytest.raises(launch.LaunchConfigError, match="index assertion"):
        launch.load_resume_identity(plan_path, asserted_gpu_index="4")
    with pytest.raises(launch.LaunchConfigError, match="UUID assertion"):
        launch.load_resume_identity(
            plan_path, asserted_gpu_uuid_output="GPU-948f0023-dd20-92a4-3f50-37d369d9bd56"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "../escape"},
        {"gpu_index": "3"},
        {"gpu_uuid": "GPU-bad-"},
        {"repository_sha": "A" * 40},
        {"active_config_sha256": "f" * 63},
    ],
)
def test_resume_rejects_malformed_plan_identity(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    write_plan(plan_path, **changes)
    with pytest.raises(launch.LaunchConfigError):
        launch.load_resume_identity(plan_path)


def test_resume_rejects_symlinked_plan(tmp_path: Path) -> None:
    launch = load_module()
    real = tmp_path / "real-plan.json"
    write_plan(real)
    link = tmp_path / "RUN_PLAN.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("test environment cannot create symlinks")
    with pytest.raises(launch.LaunchConfigError, match="symlink|cannot open"):
        launch.load_resume_identity(link)


def test_resume_rejects_duplicate_identity_keys(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    plan_path.write_text(
        '{"schema":"commu-secure-single-matrix-plan-v2",'
        '"repository_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"run_id":"main-1","gpu_index":3,"gpu_index":4,"gpu_uuid":"GPU-1234"}\n'
    )
    with pytest.raises(launch.LaunchConfigError, match="duplicate key"):
        launch.load_resume_identity(plan_path)


def test_resume_rejects_oversized_plan_before_reading(tmp_path: Path) -> None:
    launch = load_module()
    plan_path = tmp_path / "RUN_PLAN.json"
    with plan_path.open("wb") as handle:
        handle.truncate(launch.MAX_RUN_PLAN_BYTES + 1)
    with pytest.raises(launch.LaunchConfigError, match="size limit"):
        launch.load_resume_identity(plan_path)


def test_service_config_rewrites_only_gpu_and_scope_fields(tmp_path: Path) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    output = tmp_path / "gpu3.env"
    measurement = tmp_path / "measurement.env"
    write_service_template(template)
    write_measurement_config(measurement)
    original = template.read_text()
    launch.materialize_service_config(
        template,
        output,
        "3",
        "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383",
        "a" * 40,
        "/opt/vllm/bin/vllm",
        "/opt/vllm/lib",
        "/home/service/matrix-segments/attempt-000001",
        measurement,
        "a" * 40,
    )
    rendered = output.read_text()
    assert 'CUDA_VISIBLE_DEVICES="3" # portable field' in rendered
    runtime = "/home/service/matrix-segments/attempt-000001"
    assert f'RUNS_ROOT="{runtime}/runs"' in rendered
    assert f'CADDY_RUN_DIR="{runtime}/caddy"' in rendered
    assert 'VLLM_BIN="/opt/vllm/bin/vllm"' in rendered
    assert 'export LD_LIBRARY_PATH="/opt/vllm/lib"' in rendered
    expected = original.replace(
        'CUDA_VISIBLE_DEVICES="7"', 'CUDA_VISIBLE_DEVICES="3"'
    ).replace(
        'RUNS_ROOT="/old/source/gpu-7/runs"',
        f'RUNS_ROOT="{runtime}/runs"',
    ).replace(
        'CADDY_RUN_DIR="/old/source/gpu-7/caddy"',
        f'CADDY_RUN_DIR="{runtime}/caddy"',
    )
    assert rendered == expected
    assert template.read_text() == original
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_legacy_resume_preserves_exact_config_bytes_and_runtime_paths(
    tmp_path: Path,
) -> None:
    launch = load_module()
    template = tmp_path / "server.gpu3.single.env"
    measurement = tmp_path / "measurement.env"
    output = tmp_path / "service.env"
    write_service_template(template)
    write_measurement_config(measurement)
    original = template.read_bytes().replace(
        b'CUDA_VISIBLE_DEVICES="7"', b'CUDA_VISIBLE_DEVICES="3"'
    )
    template.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()

    launch.materialize_service_config(
        template,
        output,
        "3",
        "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383",
        "a" * 40,
        "/opt/vllm/bin/vllm",
        "/opt/vllm/lib",
        "/home/service/a-new-runtime-that-must-not-be-used",
        measurement,
        "a" * 40,
        digest,
    )

    assert output.read_bytes() == original
    assert b'RUNS_ROOT="/old/source/gpu-7/runs"' in output.read_bytes()
    assert b'CADDY_RUN_DIR="/old/source/gpu-7/caddy"' in output.read_bytes()


def test_resume_rejects_rendered_service_config_hash_mismatch(tmp_path: Path) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    output = tmp_path / "service.env"
    write_service_template(template)
    write_measurement_config(measurement)

    with pytest.raises(launch.LaunchConfigError, match="immutable run plan"):
        launch.materialize_service_config(
            template,
            output,
            "3",
            "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383",
            "a" * 40,
            "/opt/vllm/bin/vllm",
            "/opt/vllm/lib",
            "/home/service/stable-runtime",
            measurement,
            "a" * 40,
            "f" * 64,
        )
    assert not output.exists()


def test_new_run_runtime_root_and_config_are_stable_across_attempts(
    tmp_path: Path,
) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    first = tmp_path / "attempt-1.env"
    second = tmp_path / "attempt-2.env"
    write_service_template(template)
    write_measurement_config(measurement)
    arguments = (
        "/var/lib/commu-matrix-runtime",
        "a" * 40,
        "main-20260823t0000z",
        "3",
        "GPU-2783a59c-9574-d4f6-1d4b-bfb505341383",
    )
    runtime1 = launch.stable_service_runtime_root(*arguments)
    runtime2 = launch.stable_service_runtime_root(*arguments)
    assert runtime1 == runtime2
    assert "attempt" not in runtime1
    assert "a" * 40 in runtime1
    different_run = launch.stable_service_runtime_root(
        arguments[0], arguments[1], "main-20260823t0100z", arguments[3], arguments[4]
    )
    assert different_run != runtime1

    for output in (first, second):
        launch.materialize_service_config(
            template,
            output,
            "3",
            arguments[-1],
            "a" * 40,
            "/opt/vllm/bin/vllm",
            "/opt/vllm/lib",
            runtime1,
            measurement,
            "a" * 40,
        )
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_fd_snapshot_rejects_pathname_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    write_service_template(template)
    real_stat = launch.os.stat

    def changed_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path) == template and kwargs.get("follow_symlinks") is False:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino + 1,
            )
        return result

    monkeypatch.setattr(launch.os, "stat", changed_stat)
    with pytest.raises(launch.LaunchConfigError, match="pathname changed"):
        launch._read_stable_regular_file_bytes(
            template,
            label="service template",
            maximum_bytes=launch.MAX_SERVICE_CONFIG_BYTES,
        )


@pytest.mark.skipif(os.name != "posix", reason="dir_fd/no-follow semantics are POSIX")
def test_runtime_tree_creation_rejects_precreated_symlink(tmp_path: Path) -> None:
    launch = load_module()
    tmp_path.chmod(0o700)
    target = tmp_path / "outside"
    target.mkdir(mode=0o700)
    policy = tmp_path / "gpu-leaf"
    policy.symlink_to(target, target_is_directory=True)
    with pytest.raises(launch.LaunchConfigError, match="unsafe"):
        launch._ensure_runtime_components(
            tmp_path,
            ("gpu-leaf",),
            parent_uid=os.getuid(),
            service_uid=os.getuid(),
            service_gid=os.getgid(),
        )


@pytest.mark.skipif(os.name != "posix", reason="dir_fd ownership checks are POSIX")
def test_runtime_tree_creation_rejects_unexpected_existing_mode(tmp_path: Path) -> None:
    launch = load_module()
    tmp_path.chmod(0o700)
    leaf = tmp_path / "gpu-leaf"
    leaf.mkdir(mode=0o755)
    with pytest.raises(launch.LaunchConfigError, match="owner or mode"):
        launch._ensure_runtime_components(
            tmp_path,
            ("gpu-leaf",),
            parent_uid=os.getuid(),
            service_uid=os.getuid(),
            service_gid=os.getgid(),
        )


@pytest.mark.parametrize(
    "replacement, match",
    [
        ('VLLM_BIN="/wrong"', "VLLM_BIN"),
        ('LD_LIBRARY_PATH="/opt/vllm/lib"', "must use an export"),
        ('PARALLEL_WORKERS="2"', "PARALLEL_WORKERS"),
        ('VLLM_MODEL_REVISION="floating"', "exact commit"),
    ],
)
def test_service_config_rejects_runtime_or_matrix_drift(
    tmp_path: Path, replacement: str, match: str
) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    write_service_template(template)
    write_measurement_config(measurement)
    lines = template.read_text().splitlines()
    name = replacement.split("=", 1)[0].removeprefix("export ")
    for index, line in enumerate(lines):
        if line.removeprefix("export ").startswith(name + "="):
            lines[index] = replacement
            break
    template.write_text("\n".join(lines) + "\n")
    with pytest.raises(launch.LaunchConfigError, match=match):
        launch.materialize_service_config(
            template,
            tmp_path / "output.env",
            "3",
            "GPU-1234",
            "a" * 40,
            "/opt/vllm/bin/vllm",
            "/opt/vllm/lib",
            "/home/service/matrix-segments/attempt-000001",
            measurement,
            "a" * 40,
        )


def test_service_config_rejects_credentials_and_existing_output(tmp_path: Path) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    write_service_template(template)
    write_measurement_config(measurement)
    template.write_text(template.read_text() + '# LOCAL_VLLM_API_KEY="secret"\n')
    output = tmp_path / "output.env"
    with pytest.raises(launch.LaunchConfigError, match="credential"):
        launch.materialize_service_config(
            template, output, "3", "GPU-1234", "a" * 40,
            "/opt/vllm/bin/vllm", "/opt/vllm/lib",
            "/home/service/matrix-segments/attempt-000001", measurement, "a" * 40
        )
    write_service_template(template)
    output.write_text("preserve\n")
    with pytest.raises(launch.LaunchConfigError, match="cannot create"):
        launch.materialize_service_config(
            template, output, "3", "GPU-1234", "a" * 40,
            "/opt/vllm/bin/vllm", "/opt/vllm/lib",
            "/home/service/matrix-segments/attempt-000001", measurement, "a" * 40
        )
    assert output.read_text() == "preserve\n"


def test_service_config_must_match_validated_measurement_identity(tmp_path: Path) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    write_service_template(template)
    write_measurement_config(measurement, VLLM_MODEL="Qwen/different")
    with pytest.raises(launch.LaunchConfigError, match="service VLLM_MODEL"):
        launch.materialize_service_config(
            template, tmp_path / "output.env", "3", "GPU-1234", "a" * 40,
            "/opt/vllm/bin/vllm", "/opt/vllm/lib",
            "/home/service/matrix-segments/attempt-000001", measurement, "a" * 40,
        )


def test_service_config_rejects_invalid_measurement_config(tmp_path: Path) -> None:
    launch = load_module()
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    write_service_template(template)
    write_measurement_config(measurement, LAB_REPETITIONS="2")
    with pytest.raises(launch.LaunchConfigError, match="measurement configuration"):
        launch.materialize_service_config(
            template, tmp_path / "output.env", "3", "GPU-1234", "a" * 40,
            "/opt/vllm/bin/vllm", "/opt/vllm/lib",
            "/home/service/matrix-segments/attempt-000001", measurement, "a" * 40,
        )


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_deadline_and_resume_cli_outputs(tmp_path: Path) -> None:
    result = run_cli("deadline", "--now", "1000", "--lease", "30m")
    assert result.returncode == 0
    assert result.stdout == "1000\t30\t2200\t2800\n"
    plan = tmp_path / "RUN_PLAN.json"
    write_plan(plan)
    result = run_cli(
        "resume-identity", "--plan", str(plan), "--gpu-index", "3"
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["gpu_uuid"].startswith("GPU-2783")


def test_deadline_cli_applies_absolute_authorization_cutoff() -> None:
    result = run_cli(
        "deadline",
        "--now", "1000",
        "--lease", "110m",
        "--authorization-cutoff", "4661",
    )
    assert result.returncode == 0
    assert result.stdout == "1000\t61\t4060\t4660\n"


def test_deadline_cli_rejects_noncanonical_authorization_cutoff() -> None:
    result = run_cli(
        "deadline",
        "--now", "1000",
        "--lease", "30m",
        "--authorization-cutoff", "04661",
    )
    assert result.returncode == 2
    assert not result.stdout
    assert "canonical decimal" in result.stderr


def test_materialize_cli_output(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    measurement = tmp_path / "measurement.env"
    output = tmp_path / "gpu4.env"
    write_service_template(template)
    write_measurement_config(measurement)
    result = run_cli(
        "materialize-service-config",
        "--template", str(template), "--output", str(output),
        "--gpu-index", "4", "--gpu-uuid", "GPU-1234",
        "--source-repository-sha", "a" * 40,
        "--expected-vllm-bin", "/opt/vllm/bin/vllm",
        "--expected-ld-library-path", "/opt/vllm/lib",
        "--service-runtime-root", "/home/service/matrix-segments/attempt-000001",
        "--measurement-config", str(measurement),
        "--measurement-repository-sha", "a" * 40,
    )
    assert result.returncode == 0
    assert output.is_file()


def test_cli_reports_validation_error_to_stderr() -> None:
    result = run_cli("deadline", "--now", "01000", "--lease", "30m")
    assert result.returncode == 2
    assert not result.stdout
    assert "rejected" in result.stderr
