#!/usr/bin/env python3
"""Validate inert configuration used by the root-owned protocol-pilot release."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path, PurePosixPath


ASSIGNMENT = re.compile(
    r'^\s*(?:(export)\s+)?([A-Z_][A-Z0-9_]*)="([^"$`\\]*)"\s*(?:#.*)?$'
)
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")
GPU_UUID = re.compile(r"GPU-[0-9A-Fa-f-]+")
SAFE_INTERFACE = re.compile(r"[A-Za-z0-9_.-]{1,15}")

CREDENTIAL_NAMES = {
    "LOCAL_VLLM_API_KEY",
    "VLLM_API_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
}

# Keep this list aligned with server.lab.env.example. Unknown shell variables are
# rejected because the validated file will later be sourced by root-owned code.
ALLOWED_NAMES = {
    "LOCOMO_DATA_DIR",
    "CAPTURE_INTERFACE",
    "VLLM_HOST",
    "VLLM_PORT",
    "VLLM_SECONDARY_PORT",
    "VLLM_MODEL",
    "VLLM_SERVED_MODEL_NAME",
    "VLLM_MODEL_REVISION",
    "CUDA_VISIBLE_DEVICES",
    "PARALLEL_WORKERS",
    "VLLM_PORT_STEP",
    "TENSOR_PARALLEL_SIZE",
    "MAX_MODEL_LEN",
    "GPU_MEMORY_UTILIZATION",
    "VLLM_BIN",
    "LD_LIBRARY_PATH",
    "RUNNER_PYTHON",
    "COMPRESSOR_MODEL",
    "COMPRESSOR_DEVICE",
    "MANIFEST_PATH",
    "SUMMARY_MANIFEST_PATH",
    "MANIFEST_SHA256",
    "SUMMARY_MANIFEST_SHA256",
    "RUNS_ROOT",
    "RANDOM_SEED",
    "MAX_OUTPUT_TOKENS",
    "SUMMARY_MAX_OUTPUT_TOKENS",
    "REQUEST_TIMEOUT_SECONDS",
    "MAIN_REPETITIONS",
    "PROFILE",
    "OBSERVATION_SECONDS",
    "SUMMARY_OBSERVATION_SECONDS",
    "CAPTURE_STOP_ON_RESPONSE",
    "CAPTURE_STARTUP_DELAY_SECONDS",
    "CLIENT_NETNS",
    "HOST_VETH",
    "CLIENT_VETH",
    "HOST_VETH_CIDR",
    "CLIENT_VETH_CIDR",
    "SECURE_PROXY_HOST",
    "CAPTURE_INTERFACE_OVERRIDE",
    "CADDY_RUN_DIR",
    "PHYSICAL_LISTENER_STATE_ROOT",
    "NETWORK_MTU",
    "NETWORK_RTT_MS",
    "NETWORK_UPLINK_MBIT",
    "NETWORK_DOWNLINK_MBIT",
    "NETWORK_QUEUE_PACKETS",
    "LAB_NETWORKS",
    "LINK_CALIBRATION_SECONDS",
    "LAB_QA_SAMPLES",
    "LAB_SUMMARY_SAMPLES",
    "LAB_REPETITIONS",
    "LAB_TRANSPORTS",
    "LAB_WORKLOADS",
    "CONNECTION_MODE",
    "SESSION_TURNS",
    "SESSION_START_INTERVAL_SECONDS",
    "SESSION_BUDGET_SECONDS",
    "SESSION_SEGMENT_SECONDS",
    "SESSION_CONDITION",
    "OPENROUTER_MODEL",
    "OPENROUTER_PROVIDER",
    "OPENROUTER_BASE_URL",
    "GEMINI_MODEL",
    "GEMINI_BASE_URL",
}


class ConfigError(ValueError):
    pass


def parse_config(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"configuration is not a regular non-symlink file: {path}")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        credential_probe = raw.lstrip().lstrip("#").lstrip()
        credential_name = re.match(r"(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=", credential_probe)
        if credential_name and credential_name.group(1) in CREDENTIAL_NAMES:
            raise ConfigError(
                f"credential-looking assignment is forbidden on line {number}"
            )
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(raw)
        if not match:
            raise ConfigError(
                f"line {number} is not an inert literal NAME=\"value\" assignment"
            )
        exported, name, value = match.groups()
        if name in CREDENTIAL_NAMES:
            raise ConfigError(f"credential assignment is forbidden: {name}")
        if name not in ALLOWED_NAMES:
            raise ConfigError(f"unknown assignment is forbidden: {name}")
        if name in values:
            raise ConfigError(f"duplicate assignment: {name}")
        if exported and name != "LD_LIBRARY_PATH":
            raise ConfigError("only LD_LIBRARY_PATH may use export")
        values[name] = value
    return values


def require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise ConfigError(f"required assignment is blank or missing: {key}")
    return value


def relative_artifact(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ConfigError(f"{label} must be a normalized relative path")
    if path.parts[0] != "artifacts":
        raise ConfigError(f"{label} must be inside traffic_experiment/artifacts")
    return path


def validate_release_config(
    values: dict[str, str], repository_sha: str, release_root: Path | None
) -> None:
    if SHA1.fullmatch(repository_sha) is None:
        raise ConfigError("repository SHA must be 40 lowercase hexadecimal characters")
    expected_output = f"/var/lib/commu-protocol-pilots/{repository_sha}"
    exact = {
        "LOCOMO_DATA_DIR": "/nonexistent",
        "CAPTURE_INTERFACE": "llmhost0",
        "PARALLEL_WORKERS": "1",
        "VLLM_HOST": "127.0.0.1",
        "VLLM_PORT": "8000",
        "VLLM_SECONDARY_PORT": "8001",
        "VLLM_PORT_STEP": "1",
        "TENSOR_PARALLEL_SIZE": "1",
        "VLLM_BIN": "/usr/bin/false",
        "COMPRESSOR_DEVICE": "cpu",
        "RANDOM_SEED": "42",
        "MAX_OUTPUT_TOKENS": "4096",
        "SUMMARY_MAX_OUTPUT_TOKENS": "4096",
        "REQUEST_TIMEOUT_SECONDS": "900",
        "OBSERVATION_SECONDS": "900",
        "SUMMARY_OBSERVATION_SECONDS": "900",
        "CAPTURE_STOP_ON_RESPONSE": "true",
        "CAPTURE_STARTUP_DELAY_SECONDS": "0.5",
        "NETWORK_MTU": "1500",
        "NETWORK_RTT_MS": "40",
        "NETWORK_UPLINK_MBIT": "20",
        "NETWORK_DOWNLINK_MBIT": "50",
        "NETWORK_QUEUE_PACKETS": "1000",
        "LINK_CALIBRATION_SECONDS": "10",
        "CLIENT_NETNS": "llm-client",
        "HOST_VETH": "llmhost0",
        "CLIENT_VETH": "llmclient0",
        "HOST_VETH_CIDR": "10.200.0.1/24",
        "CLIENT_VETH_CIDR": "10.200.0.2/24",
        "SECURE_PROXY_HOST": "10.200.0.1",
        "CAPTURE_INTERFACE_OVERRIDE": "llmclient0",
        "CONNECTION_MODE": "warm",
        "OPENROUTER_MODEL": "",
        "OPENROUTER_PROVIDER": "",
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
        "GEMINI_MODEL": "",
        "GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
        "RUNS_ROOT": f"{expected_output}/runs",
        "CADDY_RUN_DIR": f"{expected_output}/caddy",
    }
    for key, expected in exact.items():
        if key not in values or values[key] != expected:
            raise ConfigError(f"{key} must equal {expected!r} for the privileged pilot")
    if values.get("LD_LIBRARY_PATH", ""):
        raise ConfigError("LD_LIBRARY_PATH must be absent or blank")
    if "RUNNER_PYTHON" in values:
        raise ConfigError("RUNNER_PYTHON must be omitted so the root-owned release runtime is used")
    gpu_index = require(values, "CUDA_VISIBLE_DEVICES")
    if re.fullmatch(r"0|[1-9][0-9]*", gpu_index) is None:
        raise ConfigError("CUDA_VISIBLE_DEVICES must contain exactly one canonical GPU index")
    if SHA1.fullmatch(require(values, "VLLM_MODEL_REVISION").lower()) is None:
        raise ConfigError("VLLM_MODEL_REVISION must be an exact commit SHA")
    if SHA256.fullmatch(require(values, "MANIFEST_SHA256")) is None:
        raise ConfigError("MANIFEST_SHA256 must be a SHA-256 digest")
    if SHA256.fullmatch(require(values, "SUMMARY_MANIFEST_SHA256")) is None:
        raise ConfigError("SUMMARY_MANIFEST_SHA256 must be a SHA-256 digest")
    for key in ("CLIENT_NETNS", "HOST_VETH", "CLIENT_VETH", "CAPTURE_INTERFACE_OVERRIDE"):
        if SAFE_INTERFACE.fullmatch(require(values, key)) is None:
            raise ConfigError(f"{key} is not a safe interface/namespace name")
    qa_path = relative_artifact(require(values, "MANIFEST_PATH"), "MANIFEST_PATH")
    summary_path = relative_artifact(
        require(values, "SUMMARY_MANIFEST_PATH"), "SUMMARY_MANIFEST_PATH"
    )
    require(values, "VLLM_MODEL")
    require(values, "VLLM_SERVED_MODEL_NAME")
    if release_root is not None:
        experiment = release_root / "repository" / "traffic_experiment"
        for relative, label, digest_key in (
            (qa_path, "QA manifest", "MANIFEST_SHA256"),
            (summary_path, "summary manifest", "SUMMARY_MANIFEST_SHA256"),
        ):
            artifact = experiment.joinpath(*relative.parts)
            if artifact.is_symlink() or not artifact.is_file():
                raise ConfigError(f"{label} is missing from the release: {artifact}")
            actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if actual != require(values, digest_key).lower():
                raise ConfigError(f"{label} digest differs from {digest_key}")


def validate_active_config(values: dict[str, str]) -> None:
    for key in (
        "PARALLEL_WORKERS",
        "CUDA_VISIBLE_DEVICES",
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
        require(values, key)


def normalize(source: Path, output: Path, repository_sha: str) -> None:
    raw = source.read_text(encoding="utf-8")
    normalized = raw.replace("@REPOSITORY_SHA@", repository_sha)
    if "@REPOSITORY_SHA@" in normalized:
        raise ConfigError("unresolved repository-SHA placeholder")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(output, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            if normalized and not normalized.endswith("\n"):
                handle.write("\n")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    values = parse_config(output)
    validate_release_config(values, repository_sha, None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("check", "check-active", "get"):
        command = subparsers.add_parser(name)
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--repository-sha", required=True)
        if name == "check":
            command.add_argument("--release-root", type=Path)
        elif name == "get":
            command.add_argument("--key", required=True)
    command = subparsers.add_parser("normalize")
    command.add_argument("--input", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--repository-sha", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.action == "normalize":
            normalize(args.input, args.output, args.repository_sha)
            return 0
        values = parse_config(args.input)
        if args.action == "check":
            validate_release_config(values, args.repository_sha, args.release_root)
            return 0
        if args.action == "check-active":
            validate_active_config(values)
            return 0
        value = values.get(args.key)
        if value is None:
            raise ConfigError(f"missing assignment: {args.key}")
        print(value)
        return 0
    except (ConfigError, OSError, UnicodeError) as exc:
        print(f"privileged pilot configuration rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
