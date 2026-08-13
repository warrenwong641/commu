from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any


MARKER_NAME = "worker-topology.json"
SCHEMA_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _absolute_lexical(path: Path) -> Path:
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _reject_symlink_components(path: Path) -> None:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"output path contains a symlink: {current}")


def _validate_topology(topology: dict[str, Any]) -> None:
    worker_count = topology.get("worker_count")
    if worker_count not in {1, 2}:
        raise ValueError("worker_count must be 1 or 2")
    if topology.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("model", "served_model_name"):
        if not isinstance(topology.get(field), str) or not topology[field]:
            raise ValueError(f"{field} must be non-empty")
    if not isinstance(topology.get("model_revision"), str):
        raise ValueError("model_revision must be a string")
    workers = topology.get("workers")
    if not isinstance(workers, list) or len(workers) != worker_count:
        raise ValueError("workers must contain exactly worker_count entries")

    gpu_indices: set[int] = set()
    gpu_uuids: set[str] = set()
    vllm_ports: set[int] = set()
    tls_ports: set[int] = set()
    http3_ports: set[int] = set()
    for expected_index, worker in enumerate(workers):
        if worker.get("worker_index") != expected_index:
            raise ValueError("workers must be ordered by contiguous worker_index")
        gpu_index = worker.get("gpu_index")
        gpu_uuid = worker.get("gpu_uuid")
        if not isinstance(gpu_index, int) or gpu_index < 0:
            raise ValueError("gpu_index must be a non-negative integer")
        if not isinstance(gpu_uuid, str) or not gpu_uuid:
            raise ValueError("gpu_uuid must be non-empty")
        secure_ports = worker.get("secure_ports")
        if not isinstance(secure_ports, dict):
            raise ValueError("secure_ports must be an object")
        ports = (
            worker.get("vllm_port"),
            secure_ports.get("tls13"),
            secure_ports.get("http3"),
        )
        if any(not isinstance(port, int) or not 1 <= port <= 65535 for port in ports):
            raise ValueError("all worker ports must be integers between 1 and 65535")
        if gpu_index in gpu_indices or gpu_uuid in gpu_uuids:
            raise ValueError("workers must use distinct physical GPUs")
        gpu_indices.add(gpu_index)
        gpu_uuids.add(gpu_uuid)
        vllm_ports.add(ports[0])
        tls_ports.add(ports[1])
        http3_ports.add(ports[2])
    if len(vllm_ports) != worker_count:
        raise ValueError("workers must use distinct vLLM ports")
    if len(tls_ports) != worker_count or len(http3_ports) != worker_count:
        raise ValueError("workers must use distinct secure ports per transport")
    all_ports = [
        port
        for worker in workers
        for port in (
            worker["vllm_port"],
            worker["secure_ports"]["tls13"],
            worker["secure_ports"]["http3"],
        )
    ]
    if len(set(all_ports)) != len(all_ports):
        raise ValueError("vLLM and secure listener ports must not collide")


def ensure_worker_topology(output_root: Path, topology: dict[str, Any]) -> Path:
    """Create or verify the immutable topology marker for an output tree."""
    _validate_topology(topology)
    root = _absolute_lexical(output_root)
    _reject_symlink_components(root)
    marker = root / MARKER_NAME

    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise ValueError(f"topology marker is not a regular file: {marker}")
        if marker.stat().st_mode & 0o222:
            raise ValueError(f"topology marker is writable: {marker}")
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read topology marker {marker}: {exc}") from exc
        if existing != topology:
            raise ValueError(
                f"worker topology mismatch for output tree {root}; "
                "use a new output tree"
            )
        return marker

    if root.exists():
        legacy_results = next(root.rglob("results.jsonl"), None)
        if legacy_results is not None:
            raise ValueError(
                f"output tree already contains results without {MARKER_NAME}: "
                f"{legacy_results}"
            )
    root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(root)

    encoded = _canonical(topology).encode("utf-8")
    temporary = root / f".{MARKER_NAME}.tmp.{os.getpid()}"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # On the Linux measurement host, make the inode read-only before
        # publishing it through the hard link. A crash or concurrent verifier
        # can therefore never observe a writable marker at the final path.
        # Windows cannot unlink a read-only hard link, so local Windows tests
        # publish, unlink the temporary name, then apply read-only mode.
        if os.name != "nt":
            os.chmod(temporary, 0o444)
        try:
            os.link(temporary, marker)
        except FileExistsError:
            return ensure_worker_topology(root, topology)
        temporary.unlink()
        if os.name == "nt":
            os.chmod(marker, 0o444)
        if os.name != "nt":
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return marker


def _parse_worker(value: str) -> dict[str, Any]:
    fields = value.split("|")
    if len(fields) != 7:
        raise argparse.ArgumentTypeError(
            "worker must be index|selector|gpu_index|gpu_uuid|vllm|tls13|http3"
        )
    try:
        worker_index, gpu_index, vllm, tls13, http3 = map(
            int,
            (fields[0], fields[2], fields[4], fields[5], fields[6]),
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError("worker numeric fields must be integers") from exc
    return {
        "worker_index": worker_index,
        "gpu_selector": fields[1],
        "gpu_index": gpu_index,
        "gpu_uuid": fields[3],
        "vllm_port": vllm,
        "secure_ports": {"tls13": tls13, "http3": http3},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="freeze measured worker topology")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--worker-count", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--served-model-name", required=True)
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--worker", action="append", type=_parse_worker, default=[])
    args = parser.parse_args()
    topology = {
        "schema_version": SCHEMA_VERSION,
        "worker_count": args.worker_count,
        "model": args.model,
        "served_model_name": args.served_model_name,
        "model_revision": args.model_revision,
        "workers": args.worker,
    }
    try:
        marker = ensure_worker_topology(args.output_root, topology)
    except ValueError as exc:
        parser.error(str(exc))
    print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
