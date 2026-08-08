#!/usr/bin/env python3
"""Static Qwen3.5/vLLM environment and local-config compatibility checker.

This module deliberately avoids importing torch, transformers, tokenizers, or
vLLM. It does not enumerate GPUs, initialize CUDA, load weights, or contact a
server or model hub.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_SPEC = Path(__file__).with_name("spec.json")
DEFAULT_LOCK = Path(__file__).with_name("requirements.lock")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", re.MULTILINE)
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Finding:
    level: str
    check: str
    message: str


def _finding(level: str, check: str, message: str) -> Finding:
    return Finding(level=level, check=check, message=message)


def load_json_object(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError(f"{path} exceeds the {max_bytes}-byte safety limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def normalize_public_version(version: str) -> str:
    """Return the PEP 440 public portion without a local ``+cu...`` label."""

    return version.split("+", 1)[0]


def check_python(spec: Mapping[str, Any], version_info: Sequence[int]) -> list[Finding]:
    expected = spec["python"]
    actual = tuple(version_info[:3])
    wanted = (expected["major"], expected["minor"], expected["patch"])
    if actual != wanted:
        return [
            _finding(
                "error",
                "python.version",
                f"expected Python {'.'.join(map(str, wanted))}, "
                f"found {'.'.join(map(str, actual))}",
            )
        ]
    return [
        _finding(
            "ok",
            "python.version",
            f"Python {'.'.join(map(str, actual))} matches the exact pin",
        )
    ]


def check_packages(
    spec: Mapping[str, Any],
    *,
    version_getter: Callable[[str], str] = importlib.metadata.version,
) -> list[Finding]:
    findings: list[Finding] = []
    for package, expected in spec["packages"].items():
        try:
            installed = version_getter(package)
        except importlib.metadata.PackageNotFoundError:
            findings.append(
                _finding("error", f"package.{package}", f"{package} is not installed")
            )
            continue
        if normalize_public_version(installed) != expected:
            findings.append(
                _finding(
                    "error",
                    f"package.{package}",
                    f"expected {expected}, found {installed}",
                )
            )
        else:
            findings.append(
                _finding(
                    "ok", f"package.{package}", f"found pinned version {installed}"
                )
            )
    return findings


def canonical_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_lock_pins(lock_path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for name, version in LOCK_PIN_RE.findall(lock_path.read_text(encoding="utf-8")):
        canonical = canonical_package_name(name)
        if canonical in pins:
            raise ValueError(f"duplicate package pin for {canonical} in {lock_path}")
        pins[canonical] = version
    if not pins:
        raise ValueError(f"no exact package pins found in {lock_path}")
    return pins


def check_lock_packages(
    lock_path: Path,
    *,
    version_getter: Callable[[str], str] = importlib.metadata.version,
) -> list[Finding]:
    try:
        pins = load_lock_pins(lock_path)
    except (OSError, ValueError) as exc:
        return [_finding("error", "packages.lock", f"cannot read lock: {exc}")]

    drift: list[str] = []
    for package, expected in pins.items():
        try:
            actual = version_getter(package)
        except importlib.metadata.PackageNotFoundError:
            drift.append(f"{package}: missing (expected {expected})")
            continue
        if actual != expected:
            drift.append(f"{package}: {actual} (expected {expected})")
    if drift:
        shown = "; ".join(drift[:12])
        suffix = f"; and {len(drift) - 12} more" if len(drift) > 12 else ""
        return [
            _finding(
                "error",
                "packages.lock",
                f"{len(drift)} of {len(pins)} locked packages drifted: {shown}{suffix}",
            )
        ]
    return [
        _finding(
            "ok", "packages.lock", f"all {len(pins)} locked package versions match"
        )
    ]


def _literal_assignments(path: Path, names: set[str]) -> dict[str, str | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, str | None] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in names:
                continue
            value = ast.literal_eval(value_node)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{target.id} in {path} is not a string or null")
            values[target.id] = value
    return values


def inspect_torch_cuda_runtime(
    spec: Mapping[str, Any],
    *,
    distribution_getter: Callable[[str], Any] = importlib.metadata.distribution,
) -> list[Finding]:
    """Read ``torch/version.py`` as text; never import torch or call CUDA."""

    try:
        distribution = distribution_getter("torch")
    except importlib.metadata.PackageNotFoundError:
        return [_finding("error", "cuda.wheel_runtime", "torch is not installed")]

    version_file = Path(distribution.locate_file("torch/version.py"))
    if not version_file.is_file():
        return [
            _finding(
                "warning",
                "cuda.wheel_runtime",
                f"cannot statically inspect missing {version_file}",
            )
        ]
    try:
        assignments = _literal_assignments(version_file, {"cuda"})
    except (OSError, SyntaxError, ValueError) as exc:
        return [
            _finding(
                "warning",
                "cuda.wheel_runtime",
                f"could not parse {version_file}: {exc}",
            )
        ]

    actual = assignments.get("cuda")
    expected = spec["cuda"]["selected_wheel_runtime"]
    if actual != expected:
        return [
            _finding(
                "error",
                "cuda.wheel_runtime",
                f"expected torch CUDA runtime {expected}, found {actual!r}",
            )
        ]
    return [
        _finding(
            "ok",
            "cuda.wheel_runtime",
            f"torch/version.py reports CUDA runtime {actual}",
        )
    ]


def resolve_config_path(value: Path) -> Path:
    return value / "config.json" if value.is_dir() else value


def _expected_cache_dir(model_id: str) -> str:
    return "models--" + model_id.replace("/", "--")


def _revision_evidence(
    config: Mapping[str, Any], config_path: Path, model: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    expected = model["revision"]
    commit_hash = config.get("_commit_hash")
    if isinstance(commit_hash, str) and commit_hash != expected:
        return commit_hash, "config _commit_hash does not match the pinned revision"

    lexical = Path(os.path.abspath(config_path))
    parts = lexical.parts
    if (
        len(parts) < 4
        or parts[-1] != "config.json"
        or parts[-3] != "snapshots"
        or parts[-4] != _expected_cache_dir(model["id"])
        or not REVISION_RE.fullmatch(parts[-2])
    ):
        return (
            None,
            "config path is not the canonical model-cache "
            "snapshots/<40hex>/config.json form",
        )
    if parts[-2] != expected:
        return parts[-2], "snapshot path does not match the pinned revision"

    model_cache = lexical.parents[2]
    try:
        resolved_target = lexical.resolve(strict=True)
        resolved_cache = model_cache.resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve config and model-cache containment: {exc}"
    if not resolved_target.is_relative_to(resolved_cache):
        return None, "resolved config target escapes the expected model cache"
    return expected, None


def check_cuda_visibility(environ: Mapping[str, str]) -> list[Finding]:
    if "CUDA_VISIBLE_DEVICES" not in environ:
        return [
            _finding(
                "error",
                "environment.cuda_visible_devices",
                "CUDA_VISIBLE_DEVICES must be explicitly set to the empty string",
            )
        ]
    value = environ["CUDA_VISIBLE_DEVICES"]
    if value != "":
        return [
            _finding(
                "error",
                "environment.cuda_visible_devices",
                "CUDA_VISIBLE_DEVICES must be empty for static preflight; "
                f"found {value!r}",
            )
        ]
    return [
        _finding(
            "ok",
            "environment.cuda_visible_devices",
            "CUDA_VISIBLE_DEVICES is explicitly empty",
        )
    ]


def check_model_config(
    spec: Mapping[str, Any], config: Mapping[str, Any], config_path: Path
) -> list[Finding]:
    expected = spec["model"]
    findings: list[Finding] = []

    architectures = config.get("architectures")
    if (
        not isinstance(architectures, list)
        or expected["architecture"] not in architectures
    ):
        findings.append(
            _finding(
                "error",
                "model.architecture",
                f"expected architectures to contain {expected['architecture']!r}; found {architectures!r}",
            )
        )
    else:
        findings.append(
            _finding(
                "ok",
                "model.architecture",
                f"found {expected['architecture']}",
            )
        )

    actual_model_type = config.get("model_type")
    if actual_model_type != expected["model_type"]:
        findings.append(
            _finding(
                "error",
                "model.model_type",
                f"expected {expected['model_type']!r}, found {actual_model_type!r}",
            )
        )
    else:
        findings.append(
            _finding("ok", "model.model_type", f"found {actual_model_type}")
        )

    text_config = config.get("text_config")
    actual_text_type = (
        text_config.get("model_type") if isinstance(text_config, dict) else None
    )
    if actual_text_type != expected["text_model_type"]:
        findings.append(
            _finding(
                "error",
                "model.text_model_type",
                f"expected {expected['text_model_type']!r}, found {actual_text_type!r}",
            )
        )
    else:
        findings.append(
            _finding("ok", "model.text_model_type", f"found {actual_text_type}")
        )

    revision, revision_error = _revision_evidence(config, config_path, expected)
    if revision_error is not None:
        findings.append(
            _finding(
                "error",
                "model.revision",
                f"exact revision is not proven: {revision_error}; "
                "config identity does not prove weight completeness",
            )
        )
    elif revision != expected["revision"]:
        findings.append(
            _finding(
                "error",
                "model.revision",
                f"expected {expected['revision']}, found revision evidence {revision}",
            )
        )
    else:
        findings.append(
            _finding("ok", "model.revision", f"found revision evidence {revision}")
        )
    return findings


def hardware_pending_findings(spec: Mapping[str, Any]) -> list[Finding]:
    cuda = spec["cuda"]
    return [
        _finding(
            "pending",
            "hardware.driver",
            "NVIDIA driver compatibility requires lab-host evidence; this static checker does not query hardware",
        ),
        _finding(
            "pending",
            "hardware.gpu",
            "GPU compute capability, VRAM capacity, dtype support, and tensor-parallel fit require lab validation; "
            f"vLLM's documented platform floor is compute capability {cuda['minimum_compute_capability']}",
        ),
    ]


def run_checks(spec_path: Path, lock_path: Path, model_config: Path) -> list[Finding]:
    spec = load_json_object(spec_path)
    config_path = resolve_config_path(model_config)
    try:
        config = load_json_object(config_path, max_bytes=MAX_CONFIG_BYTES)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return [
            *check_cuda_visibility(os.environ),
            *check_python(spec, sys.version_info),
            *check_packages(spec),
            *check_lock_packages(lock_path),
            *inspect_torch_cuda_runtime(spec),
            _finding("error", "model.config", f"cannot inspect {config_path}: {exc}"),
            *hardware_pending_findings(spec),
        ]
    return [
        *check_cuda_visibility(os.environ),
        *check_python(spec, sys.version_info),
        *check_packages(spec),
        *check_lock_packages(lock_path),
        *inspect_torch_cuda_runtime(spec),
        *check_model_config(spec, config, config_path),
        *hardware_pending_findings(spec),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "model_config",
        type=Path,
        help="local config.json or model-snapshot directory (weights are never read)",
    )
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    findings = run_checks(args.spec, args.lock, args.model_config)
    result = {
        "compatible": not any(item.level == "error" for item in findings),
        "cuda_visible_devices_explicitly_empty": (
            os.environ.get("CUDA_VISIBLE_DEVICES") == ""
        ),
        "scope": "static-package-and-config-only",
        "findings": [asdict(item) for item in findings],
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for item in findings:
            print(f"[{item.level.upper():7}] {item.check}: {item.message}")
        print(
            "PASS: static package/config checks completed; hardware validation remains pending"
            if result["compatible"]
            else "FAIL: one or more static compatibility checks failed"
        )
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
