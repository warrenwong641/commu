#!/usr/bin/env python3
"""Pure helpers for planning a configurable privileged matrix lease segment.

This module deliberately does not start services or inspect GPUs.  A privileged
launcher can pass it a single clock reading and the exact ``nvidia-smi`` UUID
output, then use the validated values to construct its runtime commands.
"""

import hashlib
import argparse
import importlib.util
import json
import os
import re
import stat
import sys
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


GPU_INDEX = re.compile(r"0|[1-9][0-9]*")
GPU_UUID = re.compile(r"GPU-(?:[0-9A-Fa-f]+-)*[0-9A-Fa-f]+")
RUN_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
LEASE_DURATION = re.compile(r"(0|[1-9][0-9]*)([mh]?)")

MIN_LEASE_MINUTES = 20
MAX_LEASE_MINUTES = 120
GRACEFUL_CLEANUP_MINUTES = 10
MAX_RUN_PLAN_BYTES = 16 * 1024 * 1024
MAX_SERVICE_CONFIG_BYTES = 1024 * 1024
RUN_PLAN_SCHEMAS = {
    "commu-secure-single-matrix-plan-v1",
    "commu-secure-single-matrix-plan-v2",
    "commu-secure-single-matrix-continuation-plan-v1",
}
CONTINUATION_PLAN_SCHEMA = "commu-secure-single-matrix-continuation-plan-v1"
SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MATRIX_RUN_ROOT = re.compile(
    r"/var/lib/commu-secure-matrix/([0-9a-f]{40})/"
    r"gpu-(0|[1-9][0-9]*)-(GPU-(?:[0-9A-Fa-f]+-)*[0-9A-Fa-f]+)/"
    r"runs/([a-z0-9][a-z0-9._-]{0,63})"
)
SERVICE_STATIC_VALUES = {
    "VLLM_HOST": "127.0.0.1",
    "VLLM_PORT": "8000",
    "VLLM_SECONDARY_PORT": "8001",
    "PARALLEL_WORKERS": "1",
    "VLLM_PORT_STEP": "1",
    "TENSOR_PARALLEL_SIZE": "1",
    "MAX_MODEL_LEN": "65536",
    "GPU_MEMORY_UTILIZATION": "0.90",
    "RANDOM_SEED": "42",
    "MAX_OUTPUT_TOKENS": "4096",
    "SUMMARY_MAX_OUTPUT_TOKENS": "4096",
    "MAIN_REPETITIONS": "3",
    "PROFILE": "main",
    "NETWORK_MTU": "1500",
    "NETWORK_RTT_MS": "40",
    "NETWORK_UPLINK_MBIT": "20",
    "NETWORK_DOWNLINK_MBIT": "50",
    "NETWORK_QUEUE_PACKETS": "1000",
    "LAB_NETWORKS": "baseline rtt realistic",
    "LAB_QA_SAMPLES": "32",
    "LAB_SUMMARY_SAMPLES": "20",
    "LAB_REPETITIONS": "3",
    "LAB_TRANSPORTS": "tls13 http3",
    "LAB_WORKLOADS": "qa summary",
    "CONNECTION_MODE": "warm",
}


class LaunchConfigError(ValueError):
    """A launcher input is ambiguous, unsafe, or inconsistent."""


@dataclass(frozen=True)
class DeadlinePlan:
    now_epoch: int
    lease_minutes: int
    cleanup_epoch: int
    hard_deadline_epoch: int


@dataclass(frozen=True)
class RunIdentity:
    schema: str
    repository_sha: str
    run_id: str
    gpu_index: int
    gpu_uuid: str
    active_config_sha256: str
    run_plan_sha256: str
    parent_matrix_root: str | None
    parent_repository_sha: str | None


def parse_gpu_index(value: str) -> int:
    """Parse a canonical, non-negative decimal GPU index."""
    if not isinstance(value, str) or GPU_INDEX.fullmatch(value) is None:
        raise LaunchConfigError("GPU index must be canonical non-negative decimal")
    return int(value)


def parse_lease_minutes(value: str) -> int:
    """Parse integer minutes, ``Nm``, or ``Nh`` within the lease policy."""
    if not isinstance(value, str):
        raise LaunchConfigError("lease duration must be a canonical duration token")
    match = LEASE_DURATION.fullmatch(value)
    if match is None:
        raise LaunchConfigError("lease duration must be integer minutes, Nm, or Nh")
    amount = int(match.group(1))
    minutes = amount * 60 if match.group(2) == "h" else amount
    if not MIN_LEASE_MINUTES <= minutes <= MAX_LEASE_MINUTES:
        raise LaunchConfigError(
            f"lease duration must be {MIN_LEASE_MINUTES}..{MAX_LEASE_MINUTES} minutes"
        )
    return minutes


def plan_deadlines(now_epoch: int, lease: str) -> DeadlinePlan:
    """Derive both deadlines from one caller-supplied clock reading."""
    if isinstance(now_epoch, bool) or not isinstance(now_epoch, int) or now_epoch < 0:
        raise LaunchConfigError("current epoch must be a non-negative integer")
    lease_minutes = parse_lease_minutes(lease)
    hard_deadline = now_epoch + lease_minutes * 60
    return DeadlinePlan(
        now_epoch=now_epoch,
        lease_minutes=lease_minutes,
        cleanup_epoch=hard_deadline - GRACEFUL_CLEANUP_MINUTES * 60,
        hard_deadline_epoch=hard_deadline,
    )


def parse_run_id(value: str) -> str:
    """Validate the canonical run identifier used as one path component."""
    if not isinstance(value, str) or RUN_ID.fullmatch(value) is None:
        raise LaunchConfigError("run ID is not canonical and safe")
    return value


def parse_gpu_uuid_line(output: str) -> str:
    """Parse exactly one canonical GPU UUID line from inventory output."""
    if not isinstance(output, str):
        raise LaunchConfigError("GPU UUID output must be text")
    lines = output.splitlines()
    if len(lines) != 1 or GPU_UUID.fullmatch(lines[0]) is None:
        raise LaunchConfigError("GPU UUID output must contain exactly one canonical line")
    return lines[0]


def parse_service_runtime_root(value: str) -> str:
    """Validate one normalized absolute POSIX directory supplied by the launcher."""
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise LaunchConfigError("service runtime root must be one absolute path")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value == "/"
        or value.startswith("//")
        or str(path) != value
        or any(part in (".", "..") for part in path.parts)
    ):
        raise LaunchConfigError("service runtime root must be normalized and absolute")
    return value


def stable_service_runtime_root(
    runtime_namespace_root: str,
    source_repository_sha: str,
    run_id: str,
    gpu_index: str,
    gpu_uuid: str,
) -> str:
    """Return the service runtime root pinned to one run and physical GPU.

    Segment/attempt identifiers are deliberately absent.  Consequently a new
    run renders byte-identical service configuration on every later resume.
    """
    root = parse_service_runtime_root(runtime_namespace_root)
    if SOURCE_SHA.fullmatch(source_repository_sha) is None:
        raise LaunchConfigError("source repository SHA must be 40 lowercase hex digits")
    canonical_run_id = parse_run_id(run_id)
    canonical_gpu_index = parse_gpu_index(gpu_index)
    canonical_gpu_uuid = parse_gpu_uuid_line(gpu_uuid)
    return (
        f"{root}/{source_repository_sha}/{canonical_run_id}/"
        f"gpu-{canonical_gpu_index}-{canonical_gpu_uuid}"
    )


def _read_stable_regular_file_bytes(
    path: Path, *, label: str, maximum_bytes: int
) -> bytes:
    if path.is_symlink():
        raise LaunchConfigError(f"{label} must not be a symlink")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LaunchConfigError(f"cannot open {label}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LaunchConfigError(f"{label} is not a regular file")
        if before.st_size > maximum_bytes:
            raise LaunchConfigError(f"{label} exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise LaunchConfigError(f"{label} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise LaunchConfigError(f"{label} changed while it was being read")
        try:
            pathname = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise LaunchConfigError(f"{label} pathname changed while reading") from exc
        if not stat.S_ISREG(pathname.st_mode) or any(
            getattr(before, field) != getattr(pathname, field)
            for field in ("st_dev", "st_ino")
        ):
            raise LaunchConfigError(f"{label} pathname changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_stable_regular_file(path: Path) -> bytes:
    return _read_stable_regular_file_bytes(
        path, label="RUN_PLAN.json", maximum_bytes=MAX_RUN_PLAN_BYTES
    )


def _parse_config_bytes(data: bytes, config, *, label: str) -> dict[str, str]:
    """Parse the exact fd-captured bytes using the release's inert grammar."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LaunchConfigError(f"{label} is not UTF-8") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        credential_probe = raw.lstrip().lstrip("#").lstrip()
        credential_name = re.match(
            r"(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=", credential_probe
        )
        if credential_name and credential_name.group(1) in config.CREDENTIAL_NAMES:
            raise LaunchConfigError(
                f"{label} has a credential-looking assignment on line {number}"
            )
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = config.ASSIGNMENT.fullmatch(raw)
        if match is None:
            raise LaunchConfigError(
                f"{label} line {number} is not an inert literal assignment"
            )
        exported, name, value = match.groups()
        if name in config.CREDENTIAL_NAMES:
            raise LaunchConfigError(f"{label} has forbidden credential: {name}")
        if name not in config.ALLOWED_NAMES:
            raise LaunchConfigError(f"{label} has forbidden assignment: {name}")
        if name in values:
            raise LaunchConfigError(f"{label} has duplicate assignment: {name}")
        if exported and name != "LD_LIBRARY_PATH":
            raise LaunchConfigError("only LD_LIBRARY_PATH may use export")
        values[name] = value
    return values


def _ensure_runtime_components(
    parent: Path,
    components: tuple[str, ...],
    *,
    parent_uid: int,
    service_uid: int,
    service_gid: int,
) -> None:
    """Create checked directory components relative to one no-follow fd."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        raise LaunchConfigError("cannot safely open runtime namespace parent") from exc
    try:
        root_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != parent_uid
            or stat.S_IMODE(root_stat.st_mode) & 0o022
        ):
            raise LaunchConfigError("runtime namespace parent has unsafe owner or mode")
        for index, component in enumerate(components):
            final_leaf = index == len(components) - 1
            expected_uid = service_uid if final_leaf else parent_uid
            expected_mode = 0o700 if final_leaf else 0o710
            created = False
            try:
                os.mkdir(component, expected_mode, dir_fd=directory_fd)
                created = True
            except FileExistsError:
                pass
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise LaunchConfigError(
                    f"runtime path component is unsafe: {component}"
                ) from exc
            try:
                if created:
                    os.fchown(child_fd, expected_uid, service_gid)
                    os.fchmod(child_fd, expected_mode)
                child_stat = os.fstat(child_fd)
                mode = stat.S_IMODE(child_stat.st_mode)
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or child_stat.st_uid != expected_uid
                    or child_stat.st_gid != service_gid
                    or mode != expected_mode
                ):
                    raise LaunchConfigError(
                        f"runtime path component has unsafe owner or mode: {component}"
                    )
            except BaseException:
                os.close(child_fd)
                raise
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)


def ensure_stable_service_runtime_root(
    runtime_namespace_root: str,
    source_repository_sha: str,
    run_id: str,
    gpu_index: str,
    gpu_uuid: str,
    service_uid: int,
    service_gid: int,
) -> str:
    """Safely create the deterministic runtime tree without following links."""
    if (
        isinstance(service_uid, bool)
        or not isinstance(service_uid, int)
        or service_uid < 0
        or isinstance(service_gid, bool)
        or not isinstance(service_gid, int)
        or service_gid < 0
    ):
        raise LaunchConfigError("service UID/GID must be non-negative integers")
    resolved = stable_service_runtime_root(
        runtime_namespace_root, source_repository_sha, run_id, gpu_index, gpu_uuid
    )
    namespace = Path(runtime_namespace_root)
    if namespace.parent != Path("/var/lib") or namespace.name != "commu-matrix-runtime":
        raise LaunchConfigError(
            "runtime namespace must be /var/lib/commu-matrix-runtime"
        )
    components = (
        namespace.name,
        source_repository_sha,
        parse_run_id(run_id),
        f"gpu-{parse_gpu_index(gpu_index)}-{parse_gpu_uuid_line(gpu_uuid)}",
    )
    _ensure_runtime_components(
        namespace.parent,
        components,
        parent_uid=0,
        service_uid=service_uid,
        service_gid=service_gid,
    )
    return resolved


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LaunchConfigError(f"RUN_PLAN.json contains duplicate key: {key}")
        result[key] = value
    return result


def load_resume_identity(
    plan_path: Path,
    *,
    asserted_gpu_index: str | None = None,
    asserted_gpu_uuid_output: str | None = None,
) -> RunIdentity:
    """Load a run's pinned identity and reject any requested GPU change.

    The assertions are optional because resume should normally inherit its GPU
    from the immutable plan.  If supplied, they are assertions only, never
    overrides.
    """
    data = _read_stable_regular_file(plan_path)
    try:
        plan = json.loads(data, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchConfigError("RUN_PLAN.json is not valid UTF-8 JSON") from exc
    if not isinstance(plan, dict):
        raise LaunchConfigError("RUN_PLAN.json must contain a JSON object")

    schema = plan.get("schema")
    repository_sha = plan.get("repository_sha")
    run_id = plan.get("run_id")
    gpu_index = plan.get("gpu_index")
    gpu_uuid = plan.get("gpu_uuid")
    active_config_sha256 = plan.get("active_config_sha256")
    parent_matrix_root: str | None = None
    parent_repository_sha: str | None = None
    if schema not in RUN_PLAN_SCHEMAS:
        raise LaunchConfigError("RUN_PLAN.json has no recognized schema identity")
    if not isinstance(repository_sha, str) or re.fullmatch(
        r"[0-9a-f]{40}", repository_sha
    ) is None:
        raise LaunchConfigError("RUN_PLAN.json has no valid repository identity")
    parse_run_id(run_id)
    if isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0:
        raise LaunchConfigError("RUN_PLAN.json has no valid GPU index")
    if not isinstance(gpu_uuid, str) or GPU_UUID.fullmatch(gpu_uuid) is None:
        raise LaunchConfigError("RUN_PLAN.json has no valid GPU UUID")
    if (
        not isinstance(active_config_sha256, str)
        or SHA256.fullmatch(active_config_sha256) is None
    ):
        raise LaunchConfigError(
            "RUN_PLAN.json has no valid active service configuration digest"
        )
    if schema == CONTINUATION_PLAN_SCHEMA:
        parent = plan.get("parent_snapshot")
        if not isinstance(parent, dict):
            raise LaunchConfigError("continuation RUN_PLAN.json has no parent snapshot")
        parent_matrix_root = parent.get("run_root")
        parent_repository_sha = parent.get("repository_sha")
        parent_match = (
            MATRIX_RUN_ROOT.fullmatch(parent_matrix_root)
            if isinstance(parent_matrix_root, str)
            else None
        )
        if (
            not isinstance(parent_matrix_root, str)
            or parent_match is None
            or not isinstance(parent_repository_sha, str)
            or SOURCE_SHA.fullmatch(parent_repository_sha) is None
            or parent_match.group(1) != parent_repository_sha
        ):
            raise LaunchConfigError("continuation parent identity is unsafe")

    if (
        asserted_gpu_index is not None
        and parse_gpu_index(asserted_gpu_index) != gpu_index
    ):
        raise LaunchConfigError("GPU index assertion does not match immutable run plan")
    if asserted_gpu_uuid_output is not None and (
        parse_gpu_uuid_line(asserted_gpu_uuid_output) != gpu_uuid
    ):
        raise LaunchConfigError("GPU UUID assertion does not match immutable run plan")

    return RunIdentity(
        schema=schema,
        repository_sha=repository_sha,
        run_id=run_id,
        gpu_index=gpu_index,
        gpu_uuid=gpu_uuid,
        active_config_sha256=active_config_sha256,
        run_plan_sha256=hashlib.sha256(data).hexdigest(),
        parent_matrix_root=parent_matrix_root,
        parent_repository_sha=parent_repository_sha,
    )


def _load_config_module():
    path = Path(__file__).with_name("privileged_matrix_config.py")
    spec = importlib.util.spec_from_file_location("_commu_privileged_matrix_config", path)
    if spec is None or spec.loader is None:
        raise LaunchConfigError("cannot load privileged matrix configuration parser")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def materialize_service_config(
    template: Path,
    output: Path,
    gpu_index: str,
    gpu_uuid: str,
    source_repository_sha: str,
    expected_vllm_bin: str,
    expected_ld_library_path: str,
    service_runtime_root: str,
    measurement_config: Path,
    measurement_repository_sha: str,
    expected_active_config_sha256: str | None = None,
) -> None:
    """Create one validated service config, preserving a pinned config exactly.

    On resume, a template whose digest already equals the immutable plan's
    active-config digest is copied byte-for-byte.  This is required for legacy
    plans whose runtime paths are part of their measurement identity.  Other
    templates are rendered using the deterministic per-run runtime root and,
    when an expected digest is supplied, must reproduce that digest exactly.
    """
    parsed_index = parse_gpu_index(gpu_index)
    parsed_uuid = parse_gpu_uuid_line(gpu_uuid)
    if SOURCE_SHA.fullmatch(source_repository_sha) is None:
        raise LaunchConfigError("source repository SHA must be 40 lowercase hex digits")
    if SOURCE_SHA.fullmatch(measurement_repository_sha) is None:
        raise LaunchConfigError(
            "measurement repository SHA must be 40 lowercase hex digits"
        )
    if not expected_vllm_bin or not expected_ld_library_path:
        raise LaunchConfigError("expected runtime paths must be non-empty")
    runtime_root = parse_service_runtime_root(service_runtime_root)
    if (
        expected_active_config_sha256 is not None
        and SHA256.fullmatch(expected_active_config_sha256) is None
    ):
        raise LaunchConfigError(
            "expected active configuration digest must be 64 lowercase hex digits"
        )

    config = _load_config_module()
    try:
        before = _read_stable_regular_file_bytes(
            template,
            label="service template",
            maximum_bytes=MAX_SERVICE_CONFIG_BYTES,
        )
        values = _parse_config_bytes(before, config, label="service template")
    except (OSError, UnicodeError, config.ConfigError) as exc:
        raise LaunchConfigError(f"service template rejected: {exc}") from exc
    try:
        raw = before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LaunchConfigError("service template is not UTF-8") from exc

    try:
        measurement_values = config.parse_config(measurement_config)
        config.validate_release_config(
            measurement_values, measurement_repository_sha, None
        )
    except (OSError, UnicodeError, config.ConfigError) as exc:
        raise LaunchConfigError(f"measurement configuration rejected: {exc}") from exc

    for name, expected in SERVICE_STATIC_VALUES.items():
        if values.get(name) != expected:
            raise LaunchConfigError(f"{name} must equal {expected!r}")
    for name in ("VLLM_MODEL", "VLLM_SERVED_MODEL_NAME"):
        if not values.get(name):
            raise LaunchConfigError(f"{name} must be non-empty")
    if SOURCE_SHA.fullmatch(values.get("VLLM_MODEL_REVISION", "")) is None:
        raise LaunchConfigError("VLLM_MODEL_REVISION must be an exact commit SHA")
    if values.get("VLLM_BIN") != expected_vllm_bin:
        raise LaunchConfigError("VLLM_BIN does not match the reviewed runtime")
    if values.get("LD_LIBRARY_PATH") != expected_ld_library_path:
        raise LaunchConfigError("LD_LIBRARY_PATH does not match the reviewed runtime")
    for name in (
        "PARALLEL_WORKERS",
        "VLLM_HOST",
        "VLLM_PORT",
        "VLLM_SECONDARY_PORT",
        "VLLM_PORT_STEP",
        "VLLM_MODEL",
        "VLLM_SERVED_MODEL_NAME",
        "VLLM_MODEL_REVISION",
        "TENSOR_PARALLEL_SIZE",
        "MAX_MODEL_LEN",
        "GPU_MEMORY_UTILIZATION",
    ):
        if values.get(name) != measurement_values.get(name):
            raise LaunchConfigError(
                f"service {name} does not match the measurement configuration"
            )
    parse_gpu_index(values.get("CUDA_VISIBLE_DEVICES", ""))
    for name in ("RUNS_ROOT", "CADDY_RUN_DIR"):
        if not values.get(name, "").startswith("/"):
            raise LaunchConfigError(f"{name} must be an absolute path")

    assignment_lines: dict[str, tuple[int, re.Match[str]]] = {}
    lines = raw.splitlines(keepends=True)
    for number, line in enumerate(lines):
        match = config.ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match is not None:
            assignment_lines[match.group(2)] = (number, match)
    ld_line = assignment_lines.get("LD_LIBRARY_PATH")
    if ld_line is None or ld_line[1].group(1) != "export":
        raise LaunchConfigError("LD_LIBRARY_PATH must use an export assignment")

    replacements = {
        "CUDA_VISIBLE_DEVICES": str(parsed_index),
        "RUNS_ROOT": f"{runtime_root}/runs",
        "CADDY_RUN_DIR": f"{runtime_root}/caddy",
    }
    preserve_exact = (
        expected_active_config_sha256 is not None
        and hashlib.sha256(before).hexdigest() == expected_active_config_sha256
    )
    if preserve_exact:
        if values.get("CUDA_VISIBLE_DEVICES") != str(parsed_index):
            raise LaunchConfigError(
                "pinned active configuration GPU differs from immutable run plan"
            )
        rendered_bytes = before
        rendered_replacements = {
            name: values[name] for name in ("CUDA_VISIBLE_DEVICES", "RUNS_ROOT", "CADDY_RUN_DIR")
        }
    else:
        for name, replacement in replacements.items():
            entry = assignment_lines.get(name)
            if entry is None:
                raise LaunchConfigError(f"service template has no {name} assignment")
            number, match = entry
            line = lines[number]
            start, end = match.span(3)
            lines[number] = line[:start] + replacement + line[end:]
        rendered_bytes = "".join(lines).encode("utf-8")
        rendered_replacements = replacements
    rendered_sha256 = hashlib.sha256(rendered_bytes).hexdigest()
    if (
        expected_active_config_sha256 is not None
        and rendered_sha256 != expected_active_config_sha256
    ):
        raise LaunchConfigError(
            "rendered service configuration digest differs from immutable run plan"
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as exc:
        raise LaunchConfigError(f"cannot create service configuration: {output}") from exc
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(rendered_bytes)
        rendered_values = _parse_config_bytes(
            rendered_bytes, config, label="rendered service configuration"
        )
        for name, replacement in rendered_replacements.items():
            if rendered_values.get(name) != replacement:
                raise LaunchConfigError(f"rendered {name} verification failed")
        for name, original in values.items():
            if name not in replacements and rendered_values.get(name) != original:
                raise LaunchConfigError(f"rendered configuration changed {name}")
    except config.ConfigError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        output.unlink(missing_ok=True)
        raise LaunchConfigError(f"rendered service configuration rejected: {exc}") from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        output.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    deadline = commands.add_parser("deadline")
    deadline.add_argument("--now", required=True)
    deadline.add_argument("--lease", required=True)
    resume = commands.add_parser("resume-identity")
    resume.add_argument("--plan", required=True, type=Path)
    resume.add_argument("--gpu-index")
    resume.add_argument("--gpu-uuid-output")
    runtime_root = commands.add_parser("stable-runtime-root")
    runtime_root.add_argument("--runtime-namespace-root", required=True)
    runtime_root.add_argument("--source-repository-sha", required=True)
    runtime_root.add_argument("--run-id", required=True)
    runtime_root.add_argument("--gpu-index", required=True)
    runtime_root.add_argument("--gpu-uuid", required=True)
    ensure_runtime_root = commands.add_parser("ensure-stable-runtime-root")
    ensure_runtime_root.add_argument("--runtime-namespace-root", required=True)
    ensure_runtime_root.add_argument("--source-repository-sha", required=True)
    ensure_runtime_root.add_argument("--run-id", required=True)
    ensure_runtime_root.add_argument("--gpu-index", required=True)
    ensure_runtime_root.add_argument("--gpu-uuid", required=True)
    ensure_runtime_root.add_argument("--service-uid", required=True)
    ensure_runtime_root.add_argument("--service-gid", required=True)
    materialize = commands.add_parser("materialize-service-config")
    materialize.add_argument("--template", required=True, type=Path)
    materialize.add_argument("--output", required=True, type=Path)
    materialize.add_argument("--gpu-index", required=True)
    materialize.add_argument("--gpu-uuid", required=True)
    materialize.add_argument("--source-repository-sha", required=True)
    materialize.add_argument("--expected-vllm-bin", required=True)
    materialize.add_argument("--expected-ld-library-path", required=True)
    materialize.add_argument("--service-runtime-root", required=True)
    materialize.add_argument("--measurement-config", required=True, type=Path)
    materialize.add_argument("--measurement-repository-sha", required=True)
    materialize.add_argument("--expected-active-config-sha256")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.action == "deadline":
            if re.fullmatch(r"0|[1-9][0-9]*", args.now) is None:
                raise LaunchConfigError("current epoch must be canonical decimal")
            plan = plan_deadlines(int(args.now), args.lease)
            print(
                f"{plan.now_epoch}\t{plan.lease_minutes}\t"
                f"{plan.cleanup_epoch}\t{plan.hard_deadline_epoch}"
            )
        elif args.action == "resume-identity":
            identity = load_resume_identity(
                args.plan,
                asserted_gpu_index=args.gpu_index,
                asserted_gpu_uuid_output=args.gpu_uuid_output,
            )
            print(json.dumps(asdict(identity), sort_keys=True, separators=(",", ":")))
        elif args.action == "stable-runtime-root":
            print(
                stable_service_runtime_root(
                    args.runtime_namespace_root,
                    args.source_repository_sha,
                    args.run_id,
                    args.gpu_index,
                    args.gpu_uuid,
                )
            )
        elif args.action == "ensure-stable-runtime-root":
            if (
                re.fullmatch(r"0|[1-9][0-9]*", args.service_uid) is None
                or re.fullmatch(r"0|[1-9][0-9]*", args.service_gid) is None
            ):
                raise LaunchConfigError("service UID/GID must be canonical decimal")
            print(
                ensure_stable_service_runtime_root(
                    args.runtime_namespace_root,
                    args.source_repository_sha,
                    args.run_id,
                    args.gpu_index,
                    args.gpu_uuid,
                    int(args.service_uid),
                    int(args.service_gid),
                )
            )
        else:
            materialize_service_config(
                args.template,
                args.output,
                args.gpu_index,
                args.gpu_uuid,
                args.source_repository_sha,
                args.expected_vllm_bin,
                args.expected_ld_library_path,
                args.service_runtime_root,
                args.measurement_config,
                args.measurement_repository_sha,
                args.expected_active_config_sha256,
            )
        return 0
    except (LaunchConfigError, OSError, UnicodeError) as exc:
        print(f"privileged matrix launch rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
