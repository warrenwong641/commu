#!/usr/bin/env python3
"""Statically validate the credential-free Qwen3.5 migration staging file."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path


EXPECTED = {
    "VLLM_MODEL": "Qwen/Qwen3.5-9B",
    "VLLM_SERVED_MODEL_NAME": "Qwen/Qwen3.5-9B",
    "VLLM_MODEL_REVISION": "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    "CUDA_VISIBLE_DEVICES": "0,1",
    "PARALLEL_WORKERS": "2",
    "VLLM_PORT": "8000",
    "VLLM_SECONDARY_PORT": "8001",
    "VLLM_PORT_STEP": "1",
    "TENSOR_PARALLEL_SIZE": "1",
    "MAX_MODEL_LEN": "65536",
    "MAX_OUTPUT_TOKENS": "4096",
    "SUMMARY_MAX_OUTPUT_TOKENS": "4096",
}
PATH_PLACEHOLDERS = {
    "VLLM_BIN": "__STAGED_VLLM_BIN__",
    "RUNNER_PYTHON": "__STAGED_RUNNER_PYTHON__",
}
ALLOWED_KEYS = {"VLLM_HOST", *EXPECTED, *PATH_PLACEHOLDERS}
ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)$")
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*="
)
CREDENTIAL_KEY_RE = re.compile(
    r"(?:^|_)(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIALS?)(?:$|_)",
    re.IGNORECASE,
)
UNSAFE_VALUE_RE = re.compile(r"[`$;&|<>]")


class ConfigError(ValueError):
    """A static staging-config validation error."""


def _decode_value(raw: str, line_number: int) -> str:
    if not raw:
        return ""
    if raw[0] in {'"', "'"}:
        quote = raw[0]
        if len(raw) < 2 or raw[-1] != quote:
            raise ConfigError(f"line {line_number}: unterminated quoted value")
        value = raw[1:-1]
        if quote in value:
            raise ConfigError(f"line {line_number}: embedded quote is not allowed")
    else:
        if any(character.isspace() for character in raw):
            raise ConfigError(
                f"line {line_number}: unquoted values cannot contain whitespace"
            )
        value = raw
    if UNSAFE_VALUE_RE.search(value):
        raise ConfigError(
            f"line {line_number}: shell expansion or control syntax is not allowed"
        )
    return value


def parse_static_env(path: Path) -> dict[str, str]:
    """Parse a strict assignment-only file without executing or sourcing it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    values: dict[str, str] = {}
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        credential_match = CREDENTIAL_ASSIGNMENT_RE.match(line)
        if credential_match and CREDENTIAL_KEY_RE.search(credential_match.group(1)):
            raise ConfigError(
                f"line {line_number}: credential assignment "
                f"{credential_match.group(1)} is forbidden"
            )
        match = ASSIGNMENT_RE.fullmatch(line)
        if not match:
            raise ConfigError(
                f"line {line_number}: expected a plain NAME=VALUE assignment"
            )
        key, raw_value = match.groups()
        if key not in ALLOWED_KEYS:
            raise ConfigError(f"line {line_number}: unknown staging key {key}")
        if key in values:
            raise ConfigError(f"line {line_number}: duplicate assignment for {key}")
        values[key] = _decode_value(raw_value, line_number)

    missing = sorted(ALLOWED_KEYS - values.keys())
    if missing:
        raise ConfigError(f"missing required assignments: {', '.join(missing)}")
    return values


def _positive_int(values: dict[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be positive")
    return value


def _validate_path(key: str, value: str) -> None:
    if value == PATH_PLACEHOLDERS[key]:
        return
    path = Path(value)
    if not path.is_absolute() or value.endswith("/"):
        raise ConfigError(
            f"{key} must be its explicit staging placeholder or an absolute staged "
            "executable path"
        )
    if ".." in path.parts:
        raise ConfigError(f"{key} cannot contain parent-directory traversal")


def validate(values: dict[str, str]) -> None:
    try:
        host = ipaddress.ip_address(values["VLLM_HOST"])
    except ValueError as exc:
        raise ConfigError("VLLM_HOST must be an explicit loopback IP address") from exc
    if not host.is_loopback:
        raise ConfigError("VLLM_HOST must be loopback-only; public bindings are unsafe")

    workers = _positive_int(values, "PARALLEL_WORKERS")
    port = _positive_int(values, "VLLM_PORT")
    secondary_port = _positive_int(values, "VLLM_SECONDARY_PORT")
    port_step = _positive_int(values, "VLLM_PORT_STEP")
    for key, parsed_port in (
        ("VLLM_PORT", port),
        ("VLLM_SECONDARY_PORT", secondary_port),
    ):
        if parsed_port > 65535:
            raise ConfigError(f"{key} must be between 1 and 65535")

    gpu_ids = values["CUDA_VISIBLE_DEVICES"].split(",")
    if len(gpu_ids) != workers or len(set(gpu_ids)) != workers:
        raise ConfigError(
            "CUDA_VISIBLE_DEVICES must map exactly one distinct GPU to each worker"
        )
    expected_secondary = port + (workers - 1) * port_step
    if secondary_port != expected_secondary:
        raise ConfigError(
            "VLLM_SECONDARY_PORT does not match the worker/port-step mapping"
        )

    for key, expected in EXPECTED.items():
        if values[key] != expected:
            raise ConfigError(f"{key} must be exactly {expected!r}")
    for key in PATH_PLACEHOLDERS:
        _validate_path(key, values[key])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="staging env example to validate")
    args = parser.parse_args(argv)
    try:
        values = parse_static_env(args.config)
        validate(values)
    except ConfigError as exc:
        print(f"QWEN35_STAGING_CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"QWEN35_STAGING_CONFIG_OK config={args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
