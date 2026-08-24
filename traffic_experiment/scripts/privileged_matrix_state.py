#!/usr/bin/env python3
"""Create and verify immutable state for the privileged full matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from pathlib import PurePosixPath


SCHEMA = "commu-secure-single-matrix-plan-v2"
LEGACY_SCHEMA = "commu-secure-single-matrix-plan-v1"
CONTINUATION_SCHEMA = "commu-secure-single-matrix-continuation-plan-v1"
CONTINUATION_SCHEMA_V2 = "commu-secure-single-matrix-continuation-plan-v2"
CONTINUATION_SCHEMAS = (CONTINUATION_SCHEMA, CONTINUATION_SCHEMA_V2)
PARENT_LEDGER_SCHEMA = "commu-matrix-parent-ledger-v1"
PARENT_LEDGER_SCHEMA_V2 = "commu-matrix-parent-ledger-v2"
GENERATION_SCHEMA = "commu-secure-single-matrix-service-generation-v1"
GENERATION_CLOSE_SCHEMA = "commu-secure-single-matrix-service-generation-close-v1"
CELL_SCHEMA = "commu-secure-single-matrix-cell-v1"
COMPLETE_SCHEMA = "commu-secure-single-matrix-complete-v2"
CONTINUATION_CELL_SCHEMA = "commu-secure-single-matrix-continuation-cell-v1"
CONTINUATION_COMPLETE_SCHEMA = "commu-secure-single-matrix-continuation-complete-v1"
NETWORKS = ("baseline", "rtt", "realistic")
TRANSPORTS = ("tls13", "http3")
WORKLOADS = (("qa", 32), ("summary", 20))
CONDITIONS = 3
REPETITIONS = 3
EXPECTED_CALLS = 2808
MEASUREMENT_PAYLOAD_FILES = frozenset(
    {
        "repository/traffic_experiment/configs/Caddyfile.single",
        "repository/traffic_experiment/scripts/privileged_matrix_request.py",
        "repository/traffic_experiment/scripts/privileged_matrix_config.py",
        "repository/traffic_experiment/scripts/caddy_readiness.py",
        "repository/traffic_experiment/scripts/11_network_condition.sh",
        "repository/traffic_experiment/scripts/lib.sh",
        "repository/traffic_experiment/scripts/protocol_admission.sh",
        "repository/traffic_experiment/.tools/caddy",
    }
)
MEASUREMENT_PAYLOAD_DIRECTORIES = (
    "repository/traffic_experiment/traffic_measure",
    "wheelhouse",
)


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"unsafe regular file: {path}")
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
        )
        if identity(before) != identity(after) or (after.st_dev, after.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise ValueError(f"file changed while hashing: {path}")
        return digest.hexdigest()
    finally:
        os.close(fd)


def measurement_payload_inventory(release_root: Path) -> dict[str, str]:
    """Project a verified release manifest onto measurement-time payloads."""
    manifest = release_root / "RELEASE_FILES.sha256"
    inventory: dict[str, str] = {}
    seen: set[str] = set()
    found_files: set[str] = set()
    found_directories: set[str] = set()
    for number, line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = re.fullmatch(r"([0-9a-f]{64})  (\./[^\n]+)", line)
        if not match:
            raise ValueError(f"malformed release manifest line {number}: {manifest}")
        relative = match.group(2)[2:]
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or relative in seen
        ):
            raise ValueError(f"unsafe/duplicate release manifest path: {relative!r}")
        seen.add(relative)

        # Bytecode is an interpreter cache, not a pinned source payload.  In
        # particular, its embedded source filename varies with release path.
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if relative in MEASUREMENT_PAYLOAD_FILES:
            inventory[relative] = match.group(1)
            found_files.add(relative)
        for directory in MEASUREMENT_PAYLOAD_DIRECTORIES:
            if relative.startswith(f"{directory}/"):
                inventory[relative] = match.group(1)
                found_directories.add(directory)

    missing = sorted(
        (MEASUREMENT_PAYLOAD_FILES - found_files)
        | (set(MEASUREMENT_PAYLOAD_DIRECTORIES) - found_directories)
    )
    if missing:
        raise ValueError(f"release manifest lacks measurement payloads: {missing}")
    return inventory


def compare_measurement_payloads(args: argparse.Namespace) -> None:
    old = measurement_payload_inventory(args.legacy_release_root)
    new = measurement_payload_inventory(args.current_release_root)
    if old != new:
        raise ValueError("legacy/current measurement payloads are not byte-identical")


def compare_continuation_payloads(args: argparse.Namespace) -> None:
    """Allow only reviewed continuation-ledger plumbing to differ."""
    old = measurement_payload_inventory(args.parent_release_root)
    new = measurement_payload_inventory(args.current_release_root)
    allowed = {
        "repository/traffic_experiment/traffic_measure/cli.py",
        "repository/traffic_experiment/traffic_measure/runner.py",
    }
    if not allowed <= set(old) or not allowed <= set(new):
        raise ValueError("continuation releases lack reviewed runner payloads")
    changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
    if changed != allowed or {key: value for key, value in old.items() if key not in allowed} != {
        key: value for key, value in new.items() if key not in allowed
    }:
        raise ValueError(
            "parent/continuation measurement payloads differ outside reviewed ledger plumbing"
        )


def safe_directory(path: Path, uid: int | None = None) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (uid is not None and metadata.st_uid != uid):
        raise ValueError(f"unsafe directory: {path}")


def read_json(path: Path, *, uid: int = 0, mode: int = 0o444) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_nlink != 1
        ):
            raise ValueError(f"unsafe immutable JSON file: {path}")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"immutable JSON changed while reading: {path}")
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        os.close(descriptor)


def publish(path: Path, value: dict) -> None:
    data = canonical(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o444, follow_symlinks=False)
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        parent_fd = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    if sha256_file(path) != hashlib.sha256(data).hexdigest():
        raise ValueError(f"published file digest mismatch: {path}")


def expected_plan(args: argparse.Namespace) -> dict:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", args.run_id) is None:
        raise ValueError("run ID is not canonical and safe")
    if args.gpu_index < 0 or re.fullmatch(r"GPU-[0-9A-Fa-f-]+", args.gpu_uuid) is None:
        raise ValueError("GPU identity is invalid")
    cells = [
        {
            "network": network,
            "transport": transport,
            "workload": workload,
            "samples": samples,
            "repetitions": REPETITIONS,
            "calls": samples * CONDITIONS * REPETITIONS,
        }
        for network in NETWORKS
        for workload, samples in WORKLOADS
        for transport in TRANSPORTS
    ]
    if sum(cell["calls"] for cell in cells) != EXPECTED_CALLS:
        raise AssertionError("fixed plan arithmetic drifted")
    topology = expected_topology(args)
    return {
        "schema": SCHEMA,
        "repository_sha": args.repository_sha,
        "pilot_repository_sha": args.pilot_repository_sha,
        "release_files_sha256": args.release_files_sha256,
        "config_sha256": args.config_sha256,
        "protocol_admission_sha256": args.admission_sha256,
        "active_config_sha256": args.active_config_sha256,
        "qa_manifest_sha256": args.qa_manifest_sha256,
        "summary_manifest_sha256": args.summary_manifest_sha256,
        "run_id": args.run_id,
        "worker_count": 1,
        "gpu_index": args.gpu_index,
        "gpu_uuid": args.gpu_uuid,
        "service_uid": args.service_uid,
        "worker_topology_sha256": hashlib.sha256(canonical(topology)).hexdigest(),
        "vllm_port": 8000,
        "networks": list(NETWORKS),
        "transports": list(TRANSPORTS),
        "workloads": [item[0] for item in WORKLOADS],
        "conditions_per_sample": CONDITIONS,
        "repetitions": REPETITIONS,
        "expected_calls": EXPECTED_CALLS,
        "cells": cells,
    }


def expected_topology(args: argparse.Namespace) -> dict:
    return {
        "schema_version": 1,
        "worker_count": 1,
        "model": args.model,
        "served_model_name": args.served_model_name,
        "model_revision": args.model_revision,
        "workers": [
            {
                "worker_index": 0,
                "gpu_selector": str(args.gpu_index),
                "gpu_index": args.gpu_index,
                "gpu_uuid": args.gpu_uuid,
                "vllm_port": 8000,
                "secure_ports": {"tls13": 8443, "http3": 8444},
            }
        ],
    }


def plan_action(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    safe_directory(root, 0)
    plan_path = root / "RUN_PLAN.json"
    topology_path = root / "worker-topology.json"
    expected = expected_plan(args)
    topology = expected_topology(args)
    if args.action == "create-plan":
        if (
            plan_path.exists()
            or plan_path.is_symlink()
            or topology_path.exists()
            or topology_path.is_symlink()
        ):
            raise ValueError("run plan/topology already exists; use resume")
        publish(topology_path, topology)
        publish(plan_path, expected)
    elif (
        read_json(plan_path) != expected
        or read_json(topology_path) != topology
        or sha256_file(topology_path) != expected["worker_topology_sha256"]
    ):
        raise ValueError(
            "immutable run plan/topology does not match live/release identity"
        )
    print(hashlib.sha256(canonical(expected)).hexdigest())


def legacy_plan_action(args: argparse.Namespace) -> None:
    """Authorize an old v1 root without altering its immutable plan.

    The old plan deliberately remains the authority for every measurement
    field.  Only the orchestration release and the lease-state generation may
    change, and both are recorded separately by ``record-generation``.
    """
    root = args.root.resolve(strict=True)
    safe_directory(root, 0)
    plan = read_json(root / "RUN_PLAN.json")
    topology = read_json(root / "worker-topology.json")
    if plan.get("schema") != LEGACY_SCHEMA:
        raise ValueError("legacy continuation requires a v1 run plan")
    expected = expected_plan(args)
    stable_keys = {
        "active_config_sha256",
        "qa_manifest_sha256",
        "summary_manifest_sha256",
        "run_id",
        "worker_count",
        "gpu_index",
        "gpu_uuid",
        "service_uid",
        "worker_topology_sha256",
        "vllm_port",
        "networks",
        "transports",
        "workloads",
        "conditions_per_sample",
        "repetitions",
        "expected_calls",
        "cells",
    }
    if any(plan.get(key) != expected.get(key) for key in stable_keys):
        raise ValueError("legacy run measurement identity differs from live service")
    if (
        plan.get("repository_sha") != args.legacy_repository_sha
        or plan.get("pilot_repository_sha") != args.legacy_repository_sha
        or plan.get("release_files_sha256") != args.legacy_release_files_sha256
        or plan.get("config_sha256") != args.legacy_config_sha256
        or plan.get("protocol_admission_sha256") != args.legacy_admission_sha256
        or re.fullmatch(r"[0-9a-f]{64}", str(plan.get("service_state_sha256", "")))
        is None
        or sha256_file(root / "worker-topology.json")
        != plan.get("worker_topology_sha256")
        or topology != expected_topology(args)
    ):
        raise ValueError("legacy immutable anchors do not match the v1 run plan")
    print(sha256_file(root / "RUN_PLAN.json"))


def continuation_plan_action(args: argparse.Namespace) -> None:
    """Create or re-verify a separate-root, cross-GPU continuation."""
    root = args.root.resolve(strict=True)
    safe_directory(root, 0)
    plan_path = root / "RUN_PLAN.json"
    topology_path = root / "worker-topology.json"
    ledger_path = root / "PARENT_LEDGER.json"
    expected = expected_plan(args)
    topology = expected_topology(args)

    if args.action == "create-continuation-plan":
        if any(
            path.exists() or path.is_symlink()
            for path in (plan_path, topology_path, ledger_path)
        ):
            raise ValueError("continuation plan/topology/ledger already exists")
        ledger = build_parent_ledger(
            args.parent_root, args.qa_manifest, args.summary_manifest
        )
        if not 0 < ledger["completed_calls"] < EXPECTED_CALLS:
            raise ValueError("parent snapshot must be incomplete with accepted progress")
        if root == Path(ledger["parent_root"]):
            raise ValueError("continuation target must use a separate run root")
        if (
            expected["gpu_index"] == ledger["parent_gpu_index"]
            or expected["gpu_uuid"] == ledger["parent_gpu_uuid"]
        ):
            raise ValueError("continuation target must use a different physical GPU")
        if expected["run_id"] == ledger["parent_run_id"]:
            raise ValueError("continuation target must use a distinct run ID")
        if (
            expected["qa_manifest_sha256"] != ledger["qa_manifest_sha256"]
            or expected["summary_manifest_sha256"]
            != ledger["summary_manifest_sha256"]
            or expected["service_uid"] != ledger["parent_service_uid"]
            or topology["model"] != ledger["parent_model"]
            or topology["served_model_name"] != ledger["parent_served_model_name"]
            or topology["model_revision"] != ledger["parent_model_revision"]
        ):
            raise ValueError("continuation target differs from frozen parent workload")
        publish(ledger_path, ledger)
        nested = ledger["schema"] == PARENT_LEDGER_SCHEMA_V2
        expected["schema"] = CONTINUATION_SCHEMA_V2 if nested else CONTINUATION_SCHEMA
        expected["analysis_role"] = "secondary-post-hoc"
        expected["source_parent_repository_sha"] = ledger.get(
            "parent_service_repository_sha", ledger["parent_repository_sha"]
        )
        if nested:
            expected["parent_run_repository_sha"] = ledger["parent_repository_sha"]
        expected["target_orchestration_repository_sha"] = expected["repository_sha"]
        expected["parent_snapshot"] = {
            "ledger_path": "PARENT_LEDGER.json",
            "ledger_sha256": sha256_file(ledger_path),
            "run_root": ledger["parent_root"],
            "run_plan_sha256": ledger["parent_run_plan_sha256"],
            "repository_sha": ledger["parent_repository_sha"],
            "run_id": ledger["parent_run_id"],
            "gpu_index": ledger["parent_gpu_index"],
            "gpu_uuid": ledger["parent_gpu_uuid"],
            "completed_calls": ledger["completed_calls"],
            "model": ledger["parent_model"],
            "served_model_name": ledger["parent_served_model_name"],
            "model_revision": ledger["parent_model_revision"],
        }
        expected["target_expected_calls"] = EXPECTED_CALLS - ledger["completed_calls"]
        publish(topology_path, topology)
        publish(plan_path, expected)
    else:
        plan = read_json(plan_path)
        if plan.get("schema") not in CONTINUATION_SCHEMAS:
            raise ValueError("run is not an explicit cross-GPU continuation")
        for key, value in expected.items():
            if key != "schema" and plan.get(key) != value:
                raise ValueError(f"continuation target identity differs at {key}")
        ledger = read_json(ledger_path)
        validate_parent_ledger_shape(ledger)
        expected_schema = (
            CONTINUATION_SCHEMA_V2
            if ledger["schema"] == PARENT_LEDGER_SCHEMA_V2
            else CONTINUATION_SCHEMA
        )
        if plan.get("schema") != expected_schema:
            raise ValueError("continuation schema does not match parent ledger generation")
        snapshot = plan.get("parent_snapshot")
        if (
            not isinstance(snapshot, dict)
            or set(snapshot)
            != {
                "ledger_path", "ledger_sha256", "run_root", "run_plan_sha256",
                "repository_sha", "run_id", "gpu_index", "gpu_uuid",
                "completed_calls", "model", "served_model_name", "model_revision",
            }
            or snapshot["ledger_path"] != "PARENT_LEDGER.json"
            or snapshot["ledger_sha256"] != sha256_file(ledger_path)
            or snapshot["run_root"] != ledger["parent_root"]
            or snapshot["run_plan_sha256"] != ledger["parent_run_plan_sha256"]
            or snapshot["repository_sha"] != ledger["parent_repository_sha"]
            or snapshot["run_id"] != ledger["parent_run_id"]
            or snapshot["gpu_index"] != ledger["parent_gpu_index"]
            or snapshot["gpu_uuid"] != ledger["parent_gpu_uuid"]
            or snapshot["completed_calls"] != ledger["completed_calls"]
            or snapshot["model"] != ledger["parent_model"]
            or snapshot["served_model_name"] != ledger["parent_served_model_name"]
            or snapshot["model_revision"] != ledger["parent_model_revision"]
            or plan.get("target_expected_calls")
            != EXPECTED_CALLS - ledger["completed_calls"]
            or plan.get("analysis_role") != "secondary-post-hoc"
            or plan.get("source_parent_repository_sha")
            != ledger.get("parent_service_repository_sha", ledger["parent_repository_sha"])
            or plan.get("target_orchestration_repository_sha")
            != expected["repository_sha"]
            or (
                plan.get("schema") == CONTINUATION_SCHEMA_V2
                and plan.get("parent_run_repository_sha")
                != ledger["parent_repository_sha"]
            )
        ):
            raise ValueError("continuation parent snapshot identity differs")
        if Path(ledger["parent_root"]) == root:
            raise ValueError("continuation target aliases its parent root")
        if (
            plan["gpu_index"] == ledger["parent_gpu_index"]
            or plan["gpu_uuid"] == ledger["parent_gpu_uuid"]
        ):
            raise ValueError("continuation target aliases its parent GPU")
        rebuilt = build_parent_ledger(
            args.parent_root, args.qa_manifest, args.summary_manifest
        )
        if rebuilt != ledger:
            raise ValueError("parent results/captures changed after continuation snapshot")
        if (
            read_json(topology_path) != topology
            or sha256_file(topology_path) != plan["worker_topology_sha256"]
        ):
            raise ValueError("continuation worker topology differs")
    print(sha256_file(plan_path))


def generation_directory(root: Path) -> Path:
    return root / "SERVICE_GENERATIONS"


def generation_records(root: Path) -> list[tuple[Path, dict]]:
    directory = generation_directory(root)
    if not directory.exists():
        return []
    safe_directory(directory, 0)
    if stat.S_IMODE(os.stat(directory, follow_symlinks=False).st_mode) != 0o755:
        raise ValueError("unsafe service-generation directory mode")
    plan = read_json(root / "RUN_PLAN.json")
    records = []
    entries = list(directory.iterdir())
    names = sorted(
        path for path in entries
        if re.fullmatch(r"[0-9]{6}\.json", path.name)
    )
    allowed = {path.name for path in names}
    for index in range(1, len(names) + 1):
        allowed.add(f"{index:06d}.state")
        closure = directory / f"{index:06d}.closed.json"
        if closure.exists():
            allowed.add(closure.name)
    if {path.name for path in entries} != allowed:
        raise ValueError("non-canonical service-generation directory entry")
    for index, path in enumerate(names, 1):
        if path.name != f"{index:06d}.json":
            raise ValueError(f"non-canonical service-generation entry: {path}")
        value = read_json(path)
        expected_keys = {
            "schema",
            "generation",
            "previous_record_sha256",
            "run_plan_sha256",
            "orchestration_repository_sha",
            "orchestration_release_sha256",
            "service_state_sha256",
            "predecessor_service_state_sha256",
            "service_state_snapshot",
            "active_config_sha256",
            "protocol_admission_sha256",
        }
        if set(value) != expected_keys or any(
            (
                value["schema"] != GENERATION_SCHEMA,
                value["generation"] != index,
                value["run_plan_sha256"] != sha256_file(root / "RUN_PLAN.json"),
                not re.fullmatch(r"[0-9a-f]{40}", value["orchestration_repository_sha"]),
                not re.fullmatch(r"[0-9a-f]{64}", value["orchestration_release_sha256"]),
                not re.fullmatch(r"[0-9a-f]{64}", value["service_state_sha256"]),
                value["predecessor_service_state_sha256"] is not None
                and not re.fullmatch(
                    r"[0-9a-f]{64}", value["predecessor_service_state_sha256"]
                ),
                not re.fullmatch(r"[0-9a-f]{64}", value["active_config_sha256"]),
                not re.fullmatch(r"[0-9a-f]{64}", value["protocol_admission_sha256"]),
            )
        ):
            raise ValueError(f"malformed service-generation record: {path}")
        snapshot = root / value["service_state_snapshot"]
        if snapshot.parent != directory or snapshot.name != f"{index:06d}.state":
            raise ValueError(f"unsafe service-state snapshot reference: {path}")
        if sha256_file(snapshot) != value["service_state_sha256"]:
            raise ValueError(f"service-state snapshot digest mismatch: {snapshot}")
        previous = None
        if index > 1:
            if not generation_is_closed(*records[-1]):
                raise ValueError("prior service-state generation is still open")
            previous = sha256_file(
                records[-1][0].with_name(records[-1][0].stem + ".closed.json")
            )
        if value["previous_record_sha256"] != previous:
            raise ValueError(f"service-generation hash chain is broken: {path}")
        expected_predecessor = (
            plan.get("service_state_sha256")
            if index == 1 and plan.get("schema") == LEGACY_SCHEMA
            else None
        )
        if value["predecessor_service_state_sha256"] != expected_predecessor:
            raise ValueError(f"service-generation predecessor is invalid: {path}")
        records.append((path, value))
    return records


def generation_is_closed(path: Path, value: dict) -> bool:
    closure = path.with_name(path.stem + ".closed.json")
    if not closure.exists():
        return False
    data = read_json(closure)
    expected_keys = {
        "schema", "generation", "activation_sha256",
        "service_state_sha256", "outcome",
    }
    if (
        set(data) != expected_keys
        or data["schema"] != GENERATION_CLOSE_SCHEMA
        or data["generation"] != value["generation"]
        or data["activation_sha256"] != sha256_file(path)
        or data["service_state_sha256"] != value["service_state_sha256"]
        or data["outcome"] not in ("ready-to-seal", "interrupted", "failed")
    ):
        raise ValueError(f"malformed service-generation closure: {closure}")
    return True


def record_generation(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
    if plan.get("schema") not in (SCHEMA, LEGACY_SCHEMA, *CONTINUATION_SCHEMAS):
        raise ValueError("unsupported run-plan schema for service generation")
    directory = generation_directory(root)
    if not directory.exists():
        directory.mkdir(mode=0o755)
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
    records = generation_records(root)
    if records and not generation_is_closed(*records[-1]):
        raise ValueError("prior service-state generation is still open")
    expected_predecessor = (
        plan.get("service_state_sha256")
        if not records and plan.get("schema") == LEGACY_SCHEMA
        else None
    )
    if expected_predecessor is not None and re.fullmatch(
        r"[0-9a-f]{64}", expected_predecessor
    ) is None:
        raise ValueError("legacy run plan has an invalid generation-zero digest")
    source = args.service_state_snapshot.resolve(strict=True)
    source_metadata = os.stat(source, follow_symlinks=False)
    if (
        not stat.S_ISREG(source_metadata.st_mode)
        or source_metadata.st_uid != 0
        or stat.S_IMODE(source_metadata.st_mode) != 0o400
        or source_metadata.st_nlink != 1
    ):
        raise ValueError("temporary service-state snapshot is unsafe")
    digest = sha256_file(source)
    if digest != args.service_state_sha256:
        raise ValueError("temporary service-state snapshot digest mismatch")
    index = len(records) + 1
    snapshot = directory / f"{index:06d}.state"
    snapshot_data = source.read_bytes()
    descriptor = os.open(
        snapshot,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o400,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(snapshot_data)
        output.flush()
        os.fsync(output.fileno())
    if sha256_file(snapshot) != digest:
        raise ValueError("published service-state snapshot digest mismatch")
    value = {
        "schema": GENERATION_SCHEMA,
        "generation": index,
        "previous_record_sha256": (
            None
            if not records
            else sha256_file(
                records[-1][0].with_name(records[-1][0].stem + ".closed.json")
            )
        ),
        "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
        "orchestration_repository_sha": args.orchestration_repository_sha,
        "orchestration_release_sha256": args.orchestration_release_sha256,
        "service_state_sha256": digest,
        "predecessor_service_state_sha256": expected_predecessor,
        "service_state_snapshot": str(snapshot.relative_to(root)),
        "active_config_sha256": args.active_config_sha256,
        "protocol_admission_sha256": args.admission_sha256,
    }
    path = directory / f"{index:06d}.json"
    try:
        publish(path, value)
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    print(sha256_file(path))


def close_generation(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    records = generation_records(root)
    if not records:
        raise ValueError("no service-state generation exists to close")
    path, value = records[-1]
    if value["service_state_sha256"] != args.service_state_sha256:
        raise ValueError("service-state generation closure digest mismatch")
    closure = path.with_name(path.stem + ".closed.json")
    if closure.exists():
        generation_is_closed(path, value)
        print(sha256_file(closure))
        return
    publish(
        closure,
        {
            "schema": GENERATION_CLOSE_SCHEMA,
            "generation": value["generation"],
            "activation_sha256": sha256_file(path),
            "service_state_sha256": args.service_state_sha256,
            "outcome": args.outcome,
        },
    )
    print(sha256_file(closure))


def recover_open_generation(args: argparse.Namespace) -> None:
    """Close only the verified last open generation after external teardown.

    Process and network teardown are deliberately proved by the privileged
    segment launcher, which also holds the global topology lock while invoking
    this command.  This state-layer operation validates the complete ledger
    chain and the expected orchestration release before publishing an
    ``interrupted`` closure.  It never changes the run plan or result rows.
    """
    root = args.root.resolve(strict=True)
    records = generation_records(root)
    if not records:
        raise ValueError("no service-state generation exists to recover")
    path, value = records[-1]
    if generation_is_closed(path, value):
        raise ValueError("last service-state generation is already closed")
    if value["orchestration_repository_sha"] != args.orchestration_repository_sha:
        raise ValueError("open generation belongs to a different orchestration release")
    closure = path.with_name(path.stem + ".closed.json")
    publish(
        closure,
        {
            "schema": GENERATION_CLOSE_SCHEMA,
            "generation": value["generation"],
            "activation_sha256": sha256_file(path),
            "service_state_sha256": value["service_state_sha256"],
            "outcome": "interrupted",
        },
    )
    print(sha256_file(closure))


def cell_path(root: Path, network: str, workload: str, transport: str) -> Path:
    if network not in NETWORKS or transport not in TRANSPORTS:
        raise ValueError("cell is outside the fixed plan")
    if workload not in dict(WORKLOADS):
        raise ValueError("cell workload is outside the fixed plan")
    return root / "cells" / network / workload / transport


def _stable_parent_bytes(path: Path, service_uid: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in (0, service_uid)
            or before.st_nlink != 1
        ):
            raise ValueError(f"unsafe parent result/capture file: {path}")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"parent file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _expected_manifest_keys(
    manifest: Path, manifest_sha256: str, service_uid: int
) -> list[tuple[str, int]]:
    supplied = manifest.absolute()
    manifest = manifest.resolve(strict=True)
    if supplied != manifest:
        raise ValueError(f"manifest path contains a symlink: {supplied}")
    data = _stable_parent_bytes(manifest, service_uid)
    if hashlib.sha256(data).hexdigest() != manifest_sha256:
        raise ValueError(f"manifest digest mismatch: {manifest}")
    request_ids = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line:
            continue
        value = json.loads(line)
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"manifest request ID is invalid at {manifest}:{number}")
        request_ids.append(request_id)
    if len(request_ids) != len(set(request_ids)):
        raise ValueError(f"manifest contains duplicate request IDs: {manifest}")
    return sorted(
        (request_id, repetition)
        for request_id in request_ids
        for repetition in range(1, REPETITIONS + 1)
    )


def _job_id(request_id: str, repetition: int) -> str:
    value = json.dumps(
        {"repetition": repetition, "request_id": request_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _validate_parent_plan(plan: dict, topology: dict) -> None:
    if plan.get("schema") not in (SCHEMA, LEGACY_SCHEMA, *CONTINUATION_SCHEMAS):
        raise ValueError("parent must be a recognized single-GPU matrix plan")
    canonical_cells = [
        {
            "network": network,
            "transport": transport,
            "workload": workload,
            "samples": samples,
            "repetitions": REPETITIONS,
            "calls": samples * CONDITIONS * REPETITIONS,
        }
        for network in NETWORKS
        for workload, samples in WORKLOADS
        for transport in TRANSPORTS
    ]
    if (
        plan.get("cells") != canonical_cells
        or plan.get("expected_calls") != EXPECTED_CALLS
        or plan.get("worker_count") != 1
        or plan.get("networks") != list(NETWORKS)
        or plan.get("transports") != list(TRANSPORTS)
        or plan.get("workloads") != [item[0] for item in WORKLOADS]
        or plan.get("conditions_per_sample") != CONDITIONS
        or plan.get("repetitions") != REPETITIONS
        or not isinstance(plan.get("service_uid"), int)
        or not isinstance(plan.get("gpu_index"), int)
        or re.fullmatch(r"GPU-[0-9A-Fa-f-]+", str(plan.get("gpu_uuid", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(plan.get("repository_sha", ""))) is None
        or topology.get("worker_count") != 1
        or topology.get("schema_version") != 1
        or not isinstance(topology.get("model"), str)
        or not topology.get("model")
        or not isinstance(topology.get("served_model_name"), str)
        or not topology.get("served_model_name")
        or re.fullmatch(r"[0-9a-f]{40}", str(topology.get("model_revision", ""))) is None
        or len(topology.get("workers", [])) != 1
        or topology["workers"][0].get("gpu_index") != plan["gpu_index"]
        or topology["workers"][0].get("gpu_uuid") != plan["gpu_uuid"]
    ):
        raise ValueError("parent run plan/topology is non-canonical")


def validate_parent_ledger_shape(ledger: dict) -> None:
    expected_top = {
        "schema", "parent_root", "parent_run_plan_sha256",
        "parent_repository_sha", "parent_run_id", "parent_gpu_index",
        "parent_gpu_uuid", "parent_service_uid", "qa_manifest_sha256",
        "summary_manifest_sha256", "qa_manifest_path", "summary_manifest_path",
        "parent_model", "parent_served_model_name", "parent_model_revision",
        "expected_keys", "completed_calls", "cells",
    }
    if ledger.get("schema") == PARENT_LEDGER_SCHEMA_V2:
        expected_top |= {
            "parent_ledger_sha256", "parent_service_repository_sha",
            "continuation_depth",
        }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != expected_top
        or ledger.get("schema") not in (PARENT_LEDGER_SCHEMA, PARENT_LEDGER_SCHEMA_V2)
        or not isinstance(ledger.get("completed_calls"), int)
        or not isinstance(ledger.get("cells"), dict)
        or not isinstance(ledger.get("expected_keys"), dict)
        or set(ledger.get("expected_keys", {})) != {"qa", "summary"}
        or re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("parent_run_plan_sha256", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(ledger.get("parent_repository_sha", ""))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("qa_manifest_sha256", ""))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("summary_manifest_sha256", ""))) is None
        or not Path(str(ledger.get("parent_root", ""))).is_absolute()
        or not Path(str(ledger.get("qa_manifest_path", ""))).is_absolute()
        or not Path(str(ledger.get("summary_manifest_path", ""))).is_absolute()
        or (
            ledger.get("schema") == PARENT_LEDGER_SCHEMA_V2
            and (
                re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("parent_ledger_sha256", ""))) is None
                or re.fullmatch(r"[0-9a-f]{40}", str(ledger.get("parent_service_repository_sha", ""))) is None
                or not isinstance(ledger.get("continuation_depth"), int)
                or ledger["continuation_depth"] < 2
            )
        )
    ):
        raise ValueError("parent ledger is malformed")
    for workload, keys in ledger["expected_keys"].items():
        normalized = []
        malformed = not isinstance(keys, list)
        for item in keys if isinstance(keys, list) else []:
            if (
                not isinstance(item, dict)
                or set(item) != {"request_id", "repetition"}
                or not isinstance(item["request_id"], str)
                or not item["request_id"]
                or not isinstance(item["repetition"], int)
                or item["repetition"] not in range(1, REPETITIONS + 1)
            ):
                malformed = True
                continue
            normalized.append((item["request_id"], item["repetition"]))
        if (
            malformed
            or len(normalized) != len(keys)
            or normalized != sorted(set(normalized))
            or len(normalized)
            != dict(WORKLOADS)[workload] * CONDITIONS * REPETITIONS
        ):
            raise ValueError(f"parent ledger expected keys are malformed: {workload}")
    expected_cell_names = {
        f"{network}/{workload}/{transport}"
        for network in NETWORKS
        for workload, _samples in WORKLOADS
        for transport in TRANSPORTS
    }
    if set(ledger["cells"]) != expected_cell_names:
        raise ValueError("parent ledger cell coverage is non-canonical")
    total = 0
    for name, cell in ledger["cells"].items():
        if not isinstance(cell, dict) or set(cell) != {
            "completed", "attempt_counts", "orphan_attempt_counts"
        }:
            raise ValueError(f"parent ledger cell is malformed: {name}")
        completed_keys = []
        for item in cell["completed"]:
            if not isinstance(item, dict) or set(item) != {
                "request_id", "repetition", "attempt", "attempt_id",
                "result_file", "result_line", "result_sha256", "capture_file",
                "capture_sha256", "manifest_sha256", "transport", "gpu_index",
                "gpu_uuid",
            }:
                raise ValueError(f"parent completed entry is malformed: {name}")
            completed_keys.append((item["request_id"], item["repetition"]))
        if completed_keys != sorted(set(completed_keys)):
            raise ValueError(f"parent completed keys are non-canonical: {name}")
        total += len(completed_keys)
        for field, key_name in (
            ("attempt_counts", "request_id"),
            ("orphan_attempt_counts", "job_id"),
        ):
            values = cell[field]
            if not isinstance(values, list) or any(
                not isinstance(item, dict)
                or set(item)
                != ({"request_id", "repetition", "count"} if key_name == "request_id" else {"job_id", "count"})
                or not isinstance(item["count"], int)
                or item["count"] < 1
                for item in values
            ):
                raise ValueError(f"parent {field} is malformed: {name}")
    if total != ledger["completed_calls"]:
        raise ValueError("parent ledger completed-call total differs")


def build_parent_ledger(
    parent_root: Path, qa_manifest: Path, summary_manifest: Path
) -> dict:
    """Validate and snapshot accepted parent progress without modifying it."""
    supplied_parent = parent_root.absolute()
    parent_root = parent_root.resolve(strict=True)
    if supplied_parent != parent_root:
        raise ValueError("parent root path contains a symlink")
    safe_directory(parent_root, 0)
    parent_plan_path = parent_root / "RUN_PLAN.json"
    topology_path = parent_root / "worker-topology.json"
    plan = read_json(parent_plan_path)
    topology = read_json(topology_path)
    _validate_parent_plan(plan, topology)
    inherited = (
        continuation_ledger(parent_root, plan, revalidate_parent=True)
        if plan.get("schema") in CONTINUATION_SCHEMAS
        else None
    )
    if (
        sha256_file(topology_path) != plan.get("worker_topology_sha256")
    ):
        raise ValueError("parent topology digest differs from run plan")
    service_uid = plan["service_uid"]
    expected_keys = {
        "qa": _expected_manifest_keys(
            qa_manifest, plan["qa_manifest_sha256"], service_uid
        ),
        "summary": _expected_manifest_keys(
            summary_manifest, plan["summary_manifest_sha256"], service_uid
        ),
    }
    if (
        len(expected_keys["qa"]) != dict(WORKLOADS)["qa"] * CONDITIONS * REPETITIONS
        or len(expected_keys["summary"])
        != dict(WORKLOADS)["summary"] * CONDITIONS * REPETITIONS
    ):
        raise ValueError("manifest cardinality differs from the fixed matrix")

    cells: dict[str, dict] = {}
    all_completed = 0
    for cell_plan in plan["cells"]:
        network = cell_plan["network"]
        workload = cell_plan["workload"]
        transport = cell_plan["transport"]
        name = f"{network}/{workload}/{transport}"
        cell = cell_path(parent_root, network, workload, transport)
        expected_set = set(expected_keys[workload])
        completed: list[dict] = []
        attempts: dict[tuple[str, int], int] = {}
        seen_attempt_ids: set[str] = set()
        seen_attempt_numbers: set[tuple[str, int, int]] = set()
        referenced_captures: set[Path] = set()
        result_rows: list[dict] = []
        if cell.exists() or cell.is_symlink():
            if cell.resolve(strict=True) != cell:
                raise ValueError(f"parent cell path contains a symlink: {cell}")
            metadata = os.stat(cell, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, service_uid)
            ):
                raise ValueError(f"unsafe parent cell directory: {cell}")
        results = cell / "results.jsonl"
        if results.exists() or results.is_symlink():
            data = _stable_parent_bytes(results, service_uid)
            for line_number, line in enumerate(data.decode("utf-8").splitlines(), 1):
                if not line:
                    continue
                row = json.loads(line)
                result_rows.append(row)
                key = (str(row.get("request_id", "")), int(row.get("repetition", 0)))
                if key not in expected_set:
                    raise ValueError(f"parent result key is outside the frozen manifest: {key}")
                attempt = int(row.get("attempt", 0))
                attempt_id = row.get("attempt_id")
                job_id = _job_id(*key)
                attempt_key = (key[0], key[1], attempt)
                if (
                    not isinstance(attempt_id, str)
                    or re.fullmatch(
                        rf"{job_id}-attempt-{attempt:03d}-[0-9a-f]{{8}}", attempt_id
                    ) is None
                    or attempt_id in seen_attempt_ids
                    or attempt_key in seen_attempt_numbers
                    or attempt < 1
                    or row.get("job_id") != job_id
                    or row.get("manifest_sha256")
                    != plan[f"{workload}_manifest_sha256"]
                    or row.get("worker_count") != 1
                    or row.get("worker_index") != 0
                    or row.get("worker_gpu_index") != plan["gpu_index"]
                    or row.get("worker_gpu_uuid") != plan["gpu_uuid"]
                    or row.get("topology_worker_index") != 0
                    or row.get("transport") != transport
                    or row.get("connection_mode") != "warm"
                    or row.get("backend_port")
                    != (8443 if transport == "tls13" else 8444)
                ):
                    raise ValueError(
                        f"parent result provenance mismatch at {results}:{line_number}"
                    )
                seen_attempt_ids.add(attempt_id)
                seen_attempt_numbers.add(attempt_key)
                attempts[key] = max(attempts.get(key, 0), attempt)

                capture_value = row.get("capture_file")
                capture = None
                if capture_value is not None:
                    capture = Path(str(capture_value))
                    capture_root = cell / "captures"
                    if (
                        not capture.is_absolute()
                        or capture.parent != capture_root
                        or capture.resolve(strict=True) != capture
                    ):
                        raise ValueError(f"parent capture escapes its cell: {capture}")
                    capture_data = _stable_parent_bytes(capture, service_uid)
                    capture_sha = hashlib.sha256(capture_data).hexdigest()
                    if capture_sha != row.get("capture_sha256"):
                        raise ValueError(f"parent capture digest mismatch: {capture}")
                    referenced_captures.add(capture)
                if row.get("completed") is True:
                    if any(
                        item["request_id"] == key[0]
                        and item["repetition"] == key[1]
                        for item in completed
                    ):
                        raise ValueError(f"parent has multiple completed rows for {key}")
                    if capture is None or capture.name != f"{attempt_id}.pcapng":
                        raise ValueError(f"parent completed capture is non-canonical: {key}")
                    completed.append(
                        {
                            "request_id": key[0],
                            "repetition": key[1],
                            "attempt": attempt,
                            "attempt_id": attempt_id,
                            "result_file": str(results),
                            "result_line": line_number,
                            "result_sha256": hashlib.sha256(canonical(row)).hexdigest(),
                            "capture_file": str(capture),
                            "capture_sha256": row["capture_sha256"],
                            "manifest_sha256": plan[f"{workload}_manifest_sha256"],
                            "transport": transport,
                            "gpu_index": plan["gpu_index"],
                            "gpu_uuid": plan["gpu_uuid"],
                        }
                    )

        orphan_counts: dict[str, int] = {}
        captures = cell / "captures"
        if captures.exists() or captures.is_symlink():
            metadata = os.stat(captures, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, service_uid)
            ):
                raise ValueError(f"unsafe parent capture directory: {captures}")
            valid_job_ids = {_job_id(*key) for key in expected_set}
            for capture in captures.iterdir():
                if capture in referenced_captures:
                    continue
                match = re.fullmatch(
                    r"([0-9a-f]{24})-attempt-([0-9]{3})-[0-9a-f]{8}\.partial\.pcapng",
                    capture.name,
                )
                if match is None or match.group(1) not in valid_job_ids:
                    raise ValueError(f"unaccounted parent capture: {capture}")
                _stable_parent_bytes(capture, service_uid)
                orphan_counts[match.group(1)] = max(
                    orphan_counts.get(match.group(1), 0), int(match.group(2))
                )
        if inherited is not None:
            validate_continuation_union(
                result_rows, inherited, name, workload, require_complete=False
            )
            inherited_cell = inherited["cells"][name]
            inherited_attempts = {
                (item["request_id"], item["repetition"]): item["count"]
                for item in inherited_cell["attempt_counts"]
            }
            inherited_orphans = {
                item["job_id"]: item["count"]
                for item in inherited_cell["orphan_attempt_counts"]
            }
            key_by_job = {_job_id(*key): key for key in expected_set}
            for job_id, count in orphan_counts.items():
                key = key_by_job[job_id]
                if count <= max(
                    inherited_attempts.get(key, 0),
                    inherited_orphans.get(job_id, 0),
                ):
                    raise ValueError(
                        f"target orphan attempt does not continue parent ledger: {key}"
                    )
            completed.extend(inherited_cell["completed"])
            for item in inherited_cell["attempt_counts"]:
                key = (item["request_id"], item["repetition"])
                attempts[key] = max(attempts.get(key, 0), item["count"])
            for item in inherited_cell["orphan_attempt_counts"]:
                orphan_counts[item["job_id"]] = max(
                    orphan_counts.get(item["job_id"], 0), item["count"]
                )
        completed.sort(key=lambda item: (item["request_id"], item["repetition"]))
        all_completed += len(completed)
        cells[name] = {
            "completed": completed,
            "attempt_counts": [
                {"request_id": key[0], "repetition": key[1], "count": count}
                for key, count in sorted(attempts.items())
            ],
            "orphan_attempt_counts": [
                {"job_id": job_id, "count": count}
                for job_id, count in sorted(orphan_counts.items())
            ],
        }
    ledger = {
        "schema": PARENT_LEDGER_SCHEMA_V2 if inherited is not None else PARENT_LEDGER_SCHEMA,
        "parent_root": str(parent_root),
        "parent_run_plan_sha256": sha256_file(parent_plan_path),
        "parent_repository_sha": plan["repository_sha"],
        "parent_run_id": plan["run_id"],
        "parent_gpu_index": plan["gpu_index"],
        "parent_gpu_uuid": plan["gpu_uuid"],
        "parent_service_uid": service_uid,
        "parent_model": topology["model"],
        "parent_served_model_name": topology["served_model_name"],
        "parent_model_revision": topology["model_revision"],
        "qa_manifest_sha256": plan["qa_manifest_sha256"],
        "summary_manifest_sha256": plan["summary_manifest_sha256"],
        "qa_manifest_path": str(qa_manifest.resolve(strict=True)),
        "summary_manifest_path": str(summary_manifest.resolve(strict=True)),
        "expected_keys": {
            workload: [
                {"request_id": key[0], "repetition": key[1]} for key in keys
            ]
            for workload, keys in expected_keys.items()
        },
        "completed_calls": all_completed,
        "cells": cells,
    }
    if inherited is not None:
        ledger.update(
            {
                "parent_ledger_sha256": sha256_file(parent_root / "PARENT_LEDGER.json"),
                "parent_service_repository_sha": plan["source_parent_repository_sha"],
                "continuation_depth": inherited.get("continuation_depth", 1) + 1,
            }
        )
    validate_parent_ledger_shape(ledger)
    return ledger


def continuation_ledger(root: Path, plan: dict, *, revalidate_parent: bool) -> dict:
    if plan.get("schema") not in CONTINUATION_SCHEMAS:
        raise ValueError("run is not a continuation")
    ledger_path = root / "PARENT_LEDGER.json"
    ledger = read_json(ledger_path)
    validate_parent_ledger_shape(ledger)
    snapshot = plan.get("parent_snapshot")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("ledger_path") != "PARENT_LEDGER.json"
        or snapshot.get("ledger_sha256") != sha256_file(ledger_path)
        or snapshot.get("run_plan_sha256") != ledger["parent_run_plan_sha256"]
        or snapshot.get("gpu_index") != ledger["parent_gpu_index"]
        or snapshot.get("gpu_uuid") != ledger["parent_gpu_uuid"]
        or snapshot.get("completed_calls") != ledger["completed_calls"]
        or snapshot.get("model") != ledger["parent_model"]
        or snapshot.get("served_model_name") != ledger["parent_served_model_name"]
        or snapshot.get("model_revision") != ledger["parent_model_revision"]
        or plan.get("source_parent_repository_sha")
        != ledger.get("parent_service_repository_sha", ledger["parent_repository_sha"])
        or plan.get("target_orchestration_repository_sha") != plan.get("repository_sha")
        or (
            plan.get("schema") == CONTINUATION_SCHEMA_V2
            and plan.get("parent_run_repository_sha") != ledger["parent_repository_sha"]
        )
    ):
        raise ValueError("continuation ledger differs from run plan")
    if revalidate_parent:
        rebuilt = build_parent_ledger(
            Path(ledger["parent_root"]),
            Path(ledger["qa_manifest_path"]),
            Path(ledger["summary_manifest_path"]),
        )
        if rebuilt != ledger:
            raise ValueError("parent changed after immutable snapshot")
    return ledger


def validate_continuation_union(
    rows: list[dict], ledger: dict, cell_name: str, workload: str,
    *, require_complete: bool = True,
) -> tuple[int, int]:
    entry = ledger["cells"][cell_name]
    parent_completed = {
        (str(item["request_id"]), int(item["repetition"]))
        for item in entry["completed"]
    }
    expected = {
        (str(item["request_id"]), int(item["repetition"]))
        for item in ledger["expected_keys"][workload]
    }
    if not parent_completed <= expected:
        raise ValueError(f"parent completed keys escape manifest: {cell_name}")
    target_completed: set[tuple[str, int]] = set()
    parent_attempts = {
        (str(item["request_id"]), int(item["repetition"])): int(item["count"])
        for item in entry["attempt_counts"]
    }
    parent_orphans = {
        str(item["job_id"]): int(item["count"])
        for item in entry["orphan_attempt_counts"]
    }
    target_attempts: set[tuple[str, int, int]] = set()
    for row in rows:
        key = (str(row.get("request_id", "")), int(row.get("repetition", 0)))
        if key not in expected:
            raise ValueError(f"target result key escapes manifest: {key}")
        if key in parent_completed:
            raise ValueError(f"target result overlaps parent completed key: {key}")
        prior_attempt = max(
            parent_attempts.get(key, 0), parent_orphans.get(_job_id(*key), 0)
        )
        attempt = int(row.get("attempt", 0))
        attempt_key = (key[0], key[1], attempt)
        attempt_id = row.get("attempt_id")
        job_id = _job_id(*key)
        if (
            attempt <= prior_attempt
            or attempt_key in target_attempts
            or row.get("job_id") != job_id
            or not isinstance(attempt_id, str)
            or re.fullmatch(
                rf"{job_id}-attempt-{attempt:03d}-[0-9a-f]{{8}}", attempt_id
            ) is None
        ):
            raise ValueError(f"target attempt does not continue parent ledger: {key}")
        target_attempts.add(attempt_key)
        if row.get("completed") is True:
            target_completed.add(key)
    if parent_completed & target_completed:
        raise ValueError(f"parent/target successful keys overlap: {cell_name}")
    if require_complete and parent_completed | target_completed != expected:
        raise ValueError(f"parent/target union is incomplete: {cell_name}")
    return len(parent_completed), len(target_completed)


def completed_rows(
    results: Path,
    manifest_sha: str,
    expected: int,
    transport: str,
    gpu_index: int,
    gpu_uuid: str,
    capture_remap: tuple[Path, Path] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    successful: dict[tuple[str, int], dict] = {}
    attempt_ids: set[str] = set()
    for number, line in enumerate(results.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        row = json.loads(line)
        attempt = row.get("attempt_id")
        if not isinstance(attempt, str) or not attempt or attempt in attempt_ids:
            raise ValueError(f"invalid/duplicate attempt at {results}:{number}")
        attempt_ids.add(attempt)
        if row.get("manifest_sha256") != manifest_sha:
            raise ValueError(f"manifest mismatch at {results}:{number}")
        if (
            row.get("worker_count") != 1
            or row.get("worker_index") != 0
            or row.get("worker_gpu_index") != gpu_index
            or row.get("worker_gpu_uuid") != gpu_uuid
            or row.get("topology_worker_index") != 0
            or row.get("transport") != transport
            or row.get("connection_mode") != "warm"
            or row.get("backend_port") != (8443 if transport == "tls13" else 8444)
        ):
            raise ValueError(f"topology mismatch at {results}:{number}")
        if row.get("completed") is True:
            key = (str(row["request_id"]), int(row["repetition"]))
            if key in successful:
                raise ValueError(f"multiple successful attempts for {key}")
            capture = Path(str(row.get("capture_file", "")))
            digest = row.get("capture_sha256")
            if capture_remap is None:
                actual_capture = capture
            else:
                recorded_root, actual_root = capture_remap
                try:
                    relative_capture = capture.relative_to(recorded_root)
                except ValueError as exc:
                    raise ValueError(
                        f"capture escapes recorded cell at {results}:{number}"
                    ) from exc
                actual_capture = actual_root / relative_capture
            resolved_capture = actual_capture.resolve(strict=True)
            if (
                not digest
                or not capture.is_absolute()
                or not resolved_capture.is_relative_to(
                    capture_remap[1] if capture_remap else results.parent
                )
                or actual_capture != resolved_capture
                or sha256_file(actual_capture) != digest
            ):
                raise ValueError(f"capture mismatch at {results}:{number}")
            successful[key] = row
        rows.append(row)
    if len(successful) != expected:
        raise ValueError(
            f"cell has {len(successful)} completed calls; expected exactly {expected}"
        )
    return rows


def inventory_tree(cell: Path, service_uid: int) -> list[dict]:
    inventory = []
    for directory, names, files in os.walk(cell, topdown=True, followlinks=False):
        current = Path(directory)
        safe_directory(current)
        if os.stat(current, follow_symlinks=False).st_uid not in (0, service_uid):
            raise ValueError(f"unsafe output directory owner: {current}")
        for name in names:
            child = current / name
            metadata = os.stat(child, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"non-directory/symlink in output tree: {child}")
        for name in files:
            path = current / name
            if path.name == "CELL_COMPLETE.json":
                continue
            metadata = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid not in (0, service_uid)
            ):
                raise ValueError(f"unsafe output file: {path}")
            inventory.append(
                {
                    "path": str(path.relative_to(cell)),
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
    return sorted(inventory, key=lambda item: item["path"])


def seal_cell(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
    original_cell = cell_path(root, args.network, args.workload, args.transport)
    safe_directory(original_cell, args.service_uid)
    original_marker = original_cell / "CELL_COMPLETE.json"
    if original_marker.exists() or original_marker.is_symlink():
        raise ValueError("cell completion marker already exists")
    full_expected = dict(WORKLOADS)[args.workload] * CONDITIONS * REPETITIONS
    continuation = plan.get("schema") in CONTINUATION_SCHEMAS
    ledger = continuation_ledger(root, plan, revalidate_parent=False) if continuation else None
    cell_name = f"{args.network}/{args.workload}/{args.transport}"
    parent_completed = (
        len(ledger["cells"][cell_name]["completed"]) if ledger is not None else 0
    )
    expected = full_expected - parent_completed
    results = original_cell / "results.jsonl"
    if expected == 0 and not results.exists() and not results.is_symlink():
        descriptor = os.open(
            results,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        os.close(descriptor)
    rows = completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
    )
    if ledger is not None:
        validate_continuation_union(rows, ledger, cell_name, args.workload)
    sealing_root = root / ".sealing"
    if not sealing_root.exists():
        sealing_root.mkdir(mode=0o700)
        os.chown(sealing_root, 0, 0)
    safe_directory(sealing_root, 0)
    if stat.S_IMODE(os.stat(sealing_root, follow_symlinks=False).st_mode) != 0o700:
        raise ValueError("unsafe sealing directory mode")
    staged = sealing_root / (
        f"{args.network}--{args.workload}--{args.transport}"
    )
    if staged.exists() or staged.is_symlink():
        raise ValueError(f"incomplete prior sealing state exists: {staged}")
    os.rename(original_cell, staged)
    cell = staged
    marker = cell / "CELL_COMPLETE.json"
    results = cell / "results.jsonl"
    rows = completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
        capture_remap=(original_cell, cell),
    )
    if ledger is not None:
        validate_continuation_union(rows, ledger, cell_name, args.workload)
    inventory = inventory_tree(cell, args.service_uid)
    for directory, names, files in os.walk(cell, topdown=False, followlinks=False):
        current = Path(directory)
        for name in files:
            path = current / name
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, 0o444, follow_symlinks=False)
        os.chown(current, 0, 0, follow_symlinks=False)
        os.chmod(current, 0o555, follow_symlinks=False)
    if inventory_tree(cell, 0) != inventory:
        raise ValueError("cell changed while ownership was being sealed")
    rows = completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
        capture_remap=(original_cell, cell),
    )
    if ledger is not None:
        validate_continuation_union(rows, ledger, cell_name, args.workload)
    os.chmod(cell, 0o755, follow_symlinks=False)
    marker_value = {
        "schema": CONTINUATION_CELL_SCHEMA if continuation else CELL_SCHEMA,
        "network": args.network,
        "workload": args.workload,
        "transport": args.transport,
        "expected_calls": full_expected,
        "manifest_sha256": args.manifest_sha256,
        "gpu_uuid": args.gpu_uuid,
        "attempt_rows": len(
            [line for line in results.read_text(encoding="utf-8").splitlines() if line]
        ),
        "inventory": inventory,
    }
    if ledger is not None:
        marker_value.update(
            {
                "parent_completed_calls": parent_completed,
                "target_completed_calls": expected,
                "parent_run_plan_sha256": ledger["parent_run_plan_sha256"],
                "parent_ledger_sha256": sha256_file(root / "PARENT_LEDGER.json"),
                "parent_gpu_index": ledger["parent_gpu_index"],
                "parent_gpu_uuid": ledger["parent_gpu_uuid"],
                "target_gpu_index": plan["gpu_index"],
                "target_gpu_uuid": plan["gpu_uuid"],
            }
        )
    publish(marker, marker_value)
    os.chmod(cell, 0o555, follow_symlinks=False)
    marker_digest = sha256_file(marker)
    os.rename(cell, original_cell)
    parent_fd = os.open(original_cell.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    print(marker_digest)


def verify_cell_marker(root: Path, plan: dict, cell_plan: dict) -> dict:
    cell = cell_path(
        root,
        cell_plan["network"],
        cell_plan["workload"],
        cell_plan["transport"],
    )
    marker = cell / "CELL_COMPLETE.json"
    data = read_json(marker)
    manifest_sha = (
        plan["qa_manifest_sha256"]
        if cell_plan["workload"] == "qa"
        else plan["summary_manifest_sha256"]
    )
    continuation = plan.get("schema") in CONTINUATION_SCHEMAS
    ledger = continuation_ledger(root, plan, revalidate_parent=False) if continuation else None
    expected_keys = {
        "schema",
        "network",
        "workload",
        "transport",
        "expected_calls",
        "manifest_sha256",
        "gpu_uuid",
        "attempt_rows",
        "inventory",
    }
    if continuation:
        expected_keys |= {
            "parent_completed_calls", "target_completed_calls",
            "parent_run_plan_sha256", "parent_ledger_sha256",
            "parent_gpu_index", "parent_gpu_uuid", "target_gpu_index",
            "target_gpu_uuid",
        }
    cell_name = (
        f'{cell_plan["network"]}/{cell_plan["workload"]}/{cell_plan["transport"]}'
    )
    parent_completed = (
        len(ledger["cells"][cell_name]["completed"]) if ledger is not None else 0
    )
    if set(data) != expected_keys or any(
        (
            data["schema"]
            != (CONTINUATION_CELL_SCHEMA if continuation else CELL_SCHEMA),
            data["network"] != cell_plan["network"],
            data["workload"] != cell_plan["workload"],
            data["transport"] != cell_plan["transport"],
            data["expected_calls"] != cell_plan["calls"],
            data["manifest_sha256"] != manifest_sha,
            data["gpu_uuid"] != plan["gpu_uuid"],
            not isinstance(data["attempt_rows"], int),
        )
    ):
        raise ValueError(f"cell marker identity differs from run plan: {marker}")
    if continuation and any(
        (
            data["parent_completed_calls"] != parent_completed,
            data["target_completed_calls"] != cell_plan["calls"] - parent_completed,
            data["parent_run_plan_sha256"] != ledger["parent_run_plan_sha256"],
            data["parent_ledger_sha256"] != sha256_file(root / "PARENT_LEDGER.json"),
            data["parent_gpu_index"] != ledger["parent_gpu_index"],
            data["parent_gpu_uuid"] != ledger["parent_gpu_uuid"],
            data["target_gpu_index"] != plan["gpu_index"],
            data["target_gpu_uuid"] != plan["gpu_uuid"],
        )
    ):
        raise ValueError(f"continuation cell provenance differs: {marker}")
    expected_inventory = data.get("inventory")
    if not isinstance(expected_inventory, list):
        raise ValueError(f"cell inventory is malformed: {marker}")
    actual_inventory = []
    for directory, names, files in os.walk(cell, topdown=True, followlinks=False):
        current = Path(directory)
        metadata = os.stat(current, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o222
        ):
            raise ValueError(f"sealed directory is unsafe: {current}")
        for name in names:
            child = current / name
            if not stat.S_ISDIR(os.stat(child, follow_symlinks=False).st_mode):
                raise ValueError(f"sealed tree contains a link/non-directory: {child}")
        for name in files:
            path = current / name
            if path == marker:
                continue
            metadata = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o222
            ):
                raise ValueError(f"sealed output is unsafe: {path}")
            actual_inventory.append(
                {
                    "path": str(path.relative_to(cell)),
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
    actual_inventory.sort(key=lambda item: item["path"])
    if actual_inventory != expected_inventory:
        raise ValueError(f"sealed cell inventory digest/coverage mismatch: {cell}")
    results = cell / "results.jsonl"
    rows = completed_rows(
        results,
        manifest_sha,
        cell_plan["calls"] - parent_completed,
        cell_plan["transport"],
        plan["gpu_index"],
        plan["gpu_uuid"],
    )
    if ledger is not None:
        validate_continuation_union(rows, ledger, cell_name, cell_plan["workload"])
    return data


def status(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
    if plan.get("schema") in CONTINUATION_SCHEMAS:
        continuation_ledger(root, plan, revalidate_parent=False)
    sealing_root = root / ".sealing"
    if sealing_root.exists():
        safe_directory(sealing_root, 0)
        leftovers = list(sealing_root.iterdir())
        if leftovers:
            raise ValueError(f"incomplete sealing state exists: {leftovers}")
    complete = []
    for cell in plan["cells"]:
        marker = cell_path(
            root, cell["network"], cell["workload"], cell["transport"]
        ) / "CELL_COMPLETE.json"
        if marker.exists():
            verify_cell_marker(root, plan, cell)
            complete.append(
                f'{cell["network"]}/{cell["workload"]}/{cell["transport"]}'
            )
        else:
            incomplete = marker.parent
            if incomplete.exists() or incomplete.is_symlink():
                safe_directory(incomplete, plan["service_uid"])
                inventory_tree(incomplete, plan["service_uid"])
    complete_marker = root / "MATRIX_COMPLETE.json"
    state = "in-progress"
    if complete_marker.exists():
        verify_matrix_marker(root, plan)
        state = "complete"
    print(
        json.dumps(
            {"completed_cells": complete, "state": state, "total_cells": 12},
            sort_keys=True,
        )
    )


def verify_matrix_marker(root: Path, plan: dict) -> dict:
    marker = root / "MATRIX_COMPLETE.json"
    data = read_json(marker)
    expected_cells = []
    for cell in plan["cells"]:
        path = cell_path(
            root, cell["network"], cell["workload"], cell["transport"]
        ) / "CELL_COMPLETE.json"
        expected_cells.append(
            {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
        )
    continuation = plan.get("schema") in CONTINUATION_SCHEMAS
    ledger = continuation_ledger(root, plan, revalidate_parent=True) if continuation else None
    expected = {
        "schema": CONTINUATION_COMPLETE_SCHEMA if continuation else COMPLETE_SCHEMA,
        "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
        "expected_calls": EXPECTED_CALLS,
        "cells": expected_cells,
        "service_generation_chain": service_generation_chain(root),
    }
    if ledger is not None:
        expected.update(
            {
                "parent_completed_calls": ledger["completed_calls"],
                "target_completed_calls": EXPECTED_CALLS - ledger["completed_calls"],
                "parent": {
                    "run_root": ledger["parent_root"],
                    "run_id": ledger["parent_run_id"],
                    "repository_sha": ledger["parent_repository_sha"],
                    "run_plan_sha256": ledger["parent_run_plan_sha256"],
                    "ledger_sha256": sha256_file(root / "PARENT_LEDGER.json"),
                    "gpu_index": ledger["parent_gpu_index"],
                    "gpu_uuid": ledger["parent_gpu_uuid"],
                },
                "target": {
                    "run_root": str(root),
                    "run_id": plan["run_id"],
                    "repository_sha": plan["repository_sha"],
                    "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
                    "gpu_index": plan["gpu_index"],
                    "gpu_uuid": plan["gpu_uuid"],
                },
            }
        )
    if data != expected:
        raise ValueError("matrix completion marker differs from sealed inventories")
    return data


def seal_matrix(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
    ledger = (
        continuation_ledger(root, plan, revalidate_parent=True)
        if plan.get("schema") in CONTINUATION_SCHEMAS
        else None
    )
    marker = root / "MATRIX_COMPLETE.json"
    if marker.exists() or marker.is_symlink():
        raise ValueError("matrix completion marker already exists")
    cells = []
    for cell in plan["cells"]:
        path = cell_path(
            root, cell["network"], cell["workload"], cell["transport"]
        ) / "CELL_COMPLETE.json"
        data = verify_cell_marker(root, plan, cell)
        cells.append({"path": str(path.relative_to(root)), "sha256": sha256_file(path)})
        if data["expected_calls"] != cell["calls"]:
            raise ValueError(f"cell plan mismatch: {path}")
    value = {
        "schema": CONTINUATION_COMPLETE_SCHEMA if ledger is not None else COMPLETE_SCHEMA,
        "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
        "expected_calls": EXPECTED_CALLS,
        "cells": cells,
        "service_generation_chain": service_generation_chain(root),
    }
    if ledger is not None:
        value.update(
            {
                "parent_completed_calls": ledger["completed_calls"],
                "target_completed_calls": EXPECTED_CALLS - ledger["completed_calls"],
                "parent": {
                    "run_root": ledger["parent_root"],
                    "run_id": ledger["parent_run_id"],
                    "repository_sha": ledger["parent_repository_sha"],
                    "run_plan_sha256": ledger["parent_run_plan_sha256"],
                    "ledger_sha256": sha256_file(root / "PARENT_LEDGER.json"),
                    "gpu_index": ledger["parent_gpu_index"],
                    "gpu_uuid": ledger["parent_gpu_uuid"],
                },
                "target": {
                    "run_root": str(root),
                    "run_id": plan["run_id"],
                    "repository_sha": plan["repository_sha"],
                    "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
                    "gpu_index": plan["gpu_index"],
                    "gpu_uuid": plan["gpu_uuid"],
                },
            }
        )
    publish(marker, value)
    verify_matrix_marker(root, plan)
    print(sha256_file(marker))


def service_generation_chain(root: Path) -> dict:
    records = generation_records(root)
    if not records or not generation_is_closed(*records[-1]):
        raise ValueError("service-generation ledger is absent or open")
    closure = records[-1][0].with_name(records[-1][0].stem + ".closed.json")
    return {"generations": len(records), "head_sha256": sha256_file(closure)}


def verify_generations(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    records = generation_records(root)
    if records and not generation_is_closed(*records[-1]):
        raise ValueError("last service-state generation is still open")
    print(json.dumps({"generations": len(records), "closed": bool(records)}))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="action", required=True)
    for action in (
        "create-plan", "verify-plan", "verify-legacy-plan",
        "create-continuation-plan", "verify-continuation-plan",
    ):
        command = commands.add_parser(action)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--repository-sha", required=True)
        command.add_argument("--pilot-repository-sha", required=True)
        command.add_argument("--release-files-sha256", required=True)
        command.add_argument("--config-sha256", required=True)
        command.add_argument("--admission-sha256", required=True)
        command.add_argument("--service-state-sha256")
        command.add_argument("--active-config-sha256", required=True)
        command.add_argument("--qa-manifest-sha256", required=True)
        command.add_argument("--summary-manifest-sha256", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--gpu-uuid", required=True)
        command.add_argument("--gpu-index", required=True, type=int)
        command.add_argument("--service-uid", required=True, type=int)
        command.add_argument("--model", required=True)
        command.add_argument("--served-model-name", required=True)
        command.add_argument("--model-revision", required=True)
        if action == "verify-legacy-plan":
            command.add_argument("--legacy-repository-sha", required=True)
            command.add_argument("--legacy-release-files-sha256", required=True)
            command.add_argument("--legacy-config-sha256", required=True)
            command.add_argument("--legacy-admission-sha256", required=True)
        if action in ("create-continuation-plan", "verify-continuation-plan"):
            command.add_argument("--parent-root", required=True, type=Path)
            command.add_argument("--qa-manifest", required=True, type=Path)
            command.add_argument("--summary-manifest", required=True, type=Path)
    command = commands.add_parser("record-generation")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--orchestration-repository-sha", required=True)
    command.add_argument("--orchestration-release-sha256", required=True)
    command.add_argument("--service-state-snapshot", required=True, type=Path)
    command.add_argument("--service-state-sha256", required=True)
    command.add_argument("--active-config-sha256", required=True)
    command.add_argument("--admission-sha256", required=True)
    command = commands.add_parser("close-generation")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--service-state-sha256", required=True)
    command.add_argument(
        "--outcome",
        choices=("ready-to-seal", "interrupted", "failed"),
        required=True,
    )
    command = commands.add_parser("recover-open-generation")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--orchestration-repository-sha", required=True)
    command = commands.add_parser("seal-cell")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--network", required=True)
    command.add_argument("--workload", required=True)
    command.add_argument("--transport", required=True)
    command.add_argument("--manifest-sha256", required=True)
    command.add_argument("--service-uid", required=True, type=int)
    command.add_argument("--gpu-uuid", required=True)
    command.add_argument("--gpu-index", required=True, type=int)
    command = commands.add_parser("compare-measurement-payloads")
    command.add_argument("--legacy-release-root", required=True, type=Path)
    command.add_argument("--current-release-root", required=True, type=Path)
    command = commands.add_parser("compare-continuation-payloads")
    command.add_argument("--parent-release-root", required=True, type=Path)
    command.add_argument("--current-release-root", required=True, type=Path)
    for action in ("status", "seal-matrix", "verify-generations"):
        command = commands.add_parser(action)
        command.add_argument("--root", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action in ("create-plan", "verify-plan"):
            plan_action(args)
        elif args.action == "verify-legacy-plan":
            legacy_plan_action(args)
        elif args.action in (
            "create-continuation-plan", "verify-continuation-plan"
        ):
            continuation_plan_action(args)
        elif args.action == "record-generation":
            record_generation(args)
        elif args.action == "close-generation":
            close_generation(args)
        elif args.action == "recover-open-generation":
            recover_open_generation(args)
        elif args.action == "seal-cell":
            seal_cell(args)
        elif args.action == "compare-measurement-payloads":
            compare_measurement_payloads(args)
        elif args.action == "compare-continuation-payloads":
            compare_continuation_payloads(args)
        elif args.action == "status":
            status(args)
        elif args.action == "verify-generations":
            verify_generations(args)
        else:
            seal_matrix(args)
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"matrix state rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
