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
GENERATION_SCHEMA = "commu-secure-single-matrix-service-generation-v1"
GENERATION_CLOSE_SCHEMA = "commu-secure-single-matrix-service-generation-close-v1"
CELL_SCHEMA = "commu-secure-single-matrix-cell-v1"
COMPLETE_SCHEMA = "commu-secure-single-matrix-complete-v2"
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
    if plan.get("schema") not in (SCHEMA, LEGACY_SCHEMA):
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
    read_json(root / "RUN_PLAN.json")
    original_cell = cell_path(root, args.network, args.workload, args.transport)
    safe_directory(original_cell, args.service_uid)
    original_marker = original_cell / "CELL_COMPLETE.json"
    if original_marker.exists() or original_marker.is_symlink():
        raise ValueError("cell completion marker already exists")
    expected = dict(WORKLOADS)[args.workload] * CONDITIONS * REPETITIONS
    results = original_cell / "results.jsonl"
    completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
    )
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
    completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
        capture_remap=(original_cell, cell),
    )
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
    completed_rows(
        results,
        args.manifest_sha256,
        expected,
        args.transport,
        args.gpu_index,
        args.gpu_uuid,
        capture_remap=(original_cell, cell),
    )
    os.chmod(cell, 0o755, follow_symlinks=False)
    marker_value = {
        "schema": CELL_SCHEMA,
        "network": args.network,
        "workload": args.workload,
        "transport": args.transport,
        "expected_calls": expected,
        "manifest_sha256": args.manifest_sha256,
        "gpu_uuid": args.gpu_uuid,
        "attempt_rows": len(
            [line for line in results.read_text(encoding="utf-8").splitlines() if line]
        ),
        "inventory": inventory,
    }
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
    if set(data) != {
        "schema",
        "network",
        "workload",
        "transport",
        "expected_calls",
        "manifest_sha256",
        "gpu_uuid",
        "attempt_rows",
        "inventory",
    } or any(
        (
            data["schema"] != CELL_SCHEMA,
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
    completed_rows(
        results,
        manifest_sha,
        cell_plan["calls"],
        cell_plan["transport"],
        plan["gpu_index"],
        plan["gpu_uuid"],
    )
    return data


def status(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
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
    expected = {
        "schema": COMPLETE_SCHEMA,
        "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
        "expected_calls": EXPECTED_CALLS,
        "cells": expected_cells,
        "service_generation_chain": service_generation_chain(root),
    }
    if data != expected:
        raise ValueError("matrix completion marker differs from sealed inventories")
    return data


def seal_matrix(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    plan = read_json(root / "RUN_PLAN.json")
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
    publish(
        marker,
        {
            "schema": COMPLETE_SCHEMA,
            "run_plan_sha256": sha256_file(root / "RUN_PLAN.json"),
            "expected_calls": EXPECTED_CALLS,
            "cells": cells,
            "service_generation_chain": service_generation_chain(root),
        },
    )
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
    for action in ("create-plan", "verify-plan", "verify-legacy-plan"):
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
