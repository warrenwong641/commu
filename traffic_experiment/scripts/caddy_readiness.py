#!/usr/bin/env python3
"""Prove TLS 1.3 and HTTP/3 listeners are reachable without an HTTP request."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import socket
import ssl
import stat
import sys
import time
from pathlib import Path


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be numeric") from exc
    if not 0.1 <= timeout <= 5.0:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 5 seconds")
    return timeout


def _safe_ca(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("CA file is not an immutable root-owned regular file")


def tls_handshake(host: str, port: int, ca_file: Path, timeout: float) -> None:
    context = ssl.create_default_context(cafile=os.fspath(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["http/1.1"])
    with socket.create_connection((host, port), timeout=timeout) as connection:
        with context.wrap_socket(connection, server_hostname=host) as secured:
            if secured.version() != "TLSv1.3":
                raise RuntimeError("listener did not negotiate TLS 1.3")
            if secured.selected_alpn_protocol() != "http/1.1":
                raise RuntimeError("listener did not negotiate HTTP/1.1 ALPN")


async def _http3_handshake(
    host: str, port: int, ca_file: Path, timeout: float
) -> None:
    from aioquic.asyncio import connect
    from aioquic.h3.connection import H3_ALPN
    from aioquic.quic.configuration import QuicConfiguration

    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=True,
        server_name=host,
    )
    configuration.load_verify_locations(cafile=os.fspath(ca_file))
    async with connect(
        host,
        port,
        configuration=configuration,
        wait_connected=False,
    ) as protocol:
        await asyncio.wait_for(protocol.wait_connected(), timeout=timeout)
        negotiated = protocol._quic.tls.alpn_negotiated
        if negotiated not in H3_ALPN:
            raise RuntimeError("listener did not negotiate HTTP/3 ALPN")


def http3_handshake(host: str, port: int, ca_file: Path, timeout: float) -> None:
    asyncio.run(_http3_handshake(host, port, ca_file, timeout))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", required=True)
    result.add_argument("--ca-file", required=True, type=Path)
    result.add_argument("--tls-port", action="append", type=_port, default=[])
    result.add_argument("--http3-port", action="append", type=_port, default=[])
    result.add_argument("--timeout-seconds", type=_timeout, default=1.0)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        host = str(ipaddress.ip_address(arguments.host))
        if not arguments.tls_port or not arguments.http3_port:
            raise ValueError("at least one TLS and one HTTP/3 port are required")
        _safe_ca(arguments.ca_file)
        deadline = time.monotonic() + arguments.timeout_seconds
        for port in arguments.tls_port:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("readiness deadline expired")
            tls_handshake(host, port, arguments.ca_file, remaining)
        for port in arguments.http3_port:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("readiness deadline expired")
            http3_handshake(host, port, arguments.ca_file, remaining)
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        print(f"Caddy readiness failed: {exc}", file=sys.stderr)
        return 1
    print(
        "CADDY_READINESS_OK "
        f"host={host} "
        f"tls_ports={','.join(map(str, arguments.tls_port))} "
        f"http3_ports={','.join(map(str, arguments.http3_port))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
