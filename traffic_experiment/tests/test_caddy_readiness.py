from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "caddy_readiness.py"


def _module():
    spec = importlib.util.spec_from_file_location("caddy_readiness_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_fake_aioquic(monkeypatch, connect, *, h3_alpn=None) -> None:
    asyncio_module = types.ModuleType("aioquic.asyncio")
    asyncio_module.connect = connect
    protocol_module = types.ModuleType("aioquic.asyncio.protocol")

    class QuicConnectionProtocol:
        pass

    protocol_module.QuicConnectionProtocol = QuicConnectionProtocol
    h3_module = types.ModuleType("aioquic.h3.connection")
    h3_module.H3_ALPN = h3_alpn or ["h3"]

    class QuicConfiguration:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.ca_file = None

        def load_verify_locations(self, *, cafile) -> None:
            self.ca_file = cafile

    configuration_module = types.ModuleType("aioquic.quic.configuration")
    configuration_module.QuicConfiguration = QuicConfiguration
    monkeypatch.setitem(sys.modules, "aioquic.asyncio", asyncio_module)
    monkeypatch.setitem(sys.modules, "aioquic.asyncio.protocol", protocol_module)
    monkeypatch.setitem(sys.modules, "aioquic.h3.connection", h3_module)
    monkeypatch.setitem(
        sys.modules, "aioquic.quic.configuration", configuration_module
    )


def test_http3_handshake_transmits_before_waiting_without_http_request(
    monkeypatch,
) -> None:
    module = _module()
    calls: list[object] = []

    class Protocol:
        def __init__(self) -> None:
            self._quic = types.SimpleNamespace(
                tls=types.SimpleNamespace(alpn_negotiated="h3")
            )

        def transmit(self) -> None:
            calls.append("transmit")

        async def wait_connected(self) -> None:
            calls.append("wait_connected")
            assert calls.index("transmit") < calls.index("wait_connected")

    protocol = Protocol()

    @contextlib.asynccontextmanager
    async def connect(host, port, **kwargs):
        calls.append(("connect", host, port, kwargs))
        yield protocol

    _install_fake_aioquic(monkeypatch, connect)
    asyncio.run(
        module._http3_handshake(
            "10.200.0.1", 8444, Path("/root/root.crt"), 0.1
        )
    )

    assert calls[0][0:3] == ("connect", "10.200.0.1", 8444)
    assert calls[0][3]["wait_connected"] is False
    protocol_class = calls[0][3]["create_protocol"]
    assert issubclass(
        protocol_class,
        sys.modules["aioquic.asyncio.protocol"].QuicConnectionProtocol,
    )
    assert protocol_class.quic_event_received(object(), object()) is None
    assert calls[0][3]["configuration"].kwargs == {
        "alpn_protocols": ["h3"],
        "is_client": True,
        "server_name": "10.200.0.1",
    }
    assert calls[0][3]["configuration"].ca_file == os.fspath(
        Path("/root/root.crt")
    )
    assert calls[1:] == ["transmit", "wait_connected"]

    source = SCRIPT.read_text(encoding="utf-8")
    assert ".request(" not in source
    assert ".get(" not in source
    assert ".post(" not in source


def test_http3_handshake_reports_connection_failure(monkeypatch) -> None:
    module = _module()

    class Protocol:
        def __init__(self) -> None:
            self._quic = types.SimpleNamespace(
                tls=types.SimpleNamespace(alpn_negotiated=None)
            )

        def transmit(self) -> None:
            pass

        async def wait_connected(self) -> None:
            raise ConnectionError()

    @contextlib.asynccontextmanager
    async def connect(_host, _port, **_kwargs):
        yield Protocol()

    _install_fake_aioquic(monkeypatch, connect)
    with pytest.raises(
        ConnectionError,
        match=r"HTTP/3 handshake to 10\.200\.0\.1:8444 failed: ConnectionError",
    ):
        asyncio.run(
            module._http3_handshake(
                "10.200.0.1", 8444, Path("/root/root.crt"), 0.1
            )
        )


def test_http3_handshake_timeout_closes_and_retrieves_connection_failure(
    monkeypatch,
) -> None:
    module = _module()
    closed = asyncio.Event()
    calls: list[str] = []

    class Protocol:
        def __init__(self) -> None:
            self._quic = types.SimpleNamespace(
                tls=types.SimpleNamespace(alpn_negotiated=None)
            )

        def transmit(self) -> None:
            calls.append("transmit")

        async def wait_connected(self) -> None:
            calls.append("wait_connected")
            await closed.wait()
            raise ConnectionError()

        def close(self, *, error_code, reason_phrase) -> None:
            calls.append(f"close:{error_code}:{reason_phrase}")
            closed.set()

        async def wait_closed(self) -> None:
            calls.append("wait_closed")
            await asyncio.sleep(0)

    @contextlib.asynccontextmanager
    async def connect(_host, _port, **_kwargs):
        yield Protocol()

    _install_fake_aioquic(monkeypatch, connect)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        with pytest.raises(
            TimeoutError,
            match=(
                r"HTTP/3 handshake to 10\.200\.0\.1:8444 timed out "
                r"after 0\.001 seconds"
            ),
        ):
            await module._http3_handshake(
                "10.200.0.1", 8444, Path("/root/root.crt"), 0.001
            )
        await asyncio.sleep(0)
        assert unhandled == []

    asyncio.run(run())
    assert calls == [
        "transmit",
        "wait_connected",
        "close:0:Caddy readiness handshake timed out",
        "wait_closed",
    ]


def test_readiness_checks_every_tls_and_http3_listener_without_http_request(
    monkeypatch,
) -> None:
    module = _module()
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(module, "_safe_ca", lambda _path: None)
    monkeypatch.setattr(
        module,
        "tls_handshake",
        lambda host, port, _ca, _timeout: calls.append(("tls", host, port)),
    )
    monkeypatch.setattr(
        module,
        "http3_handshake",
        lambda host, port, _ca, _timeout: calls.append(("http3", host, port)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--host",
            "10.200.0.1",
            "--ca-file",
            "/root/root.crt",
            "--tls-port",
            "8443",
            "--tls-port",
            "8543",
            "--http3-port",
            "8444",
            "--http3-port",
            "8544",
            "--timeout-seconds",
            "1",
        ],
    )

    assert module.main() == 0
    assert calls == [
        ("tls", "10.200.0.1", 8443),
        ("tls", "10.200.0.1", 8543),
        ("http3", "10.200.0.1", 8444),
        ("http3", "10.200.0.1", 8544),
    ]
    source = SCRIPT.read_text(encoding="utf-8")
    assert ".request(" not in source
    assert ".get(" not in source
    assert ".post(" not in source


def test_readiness_requires_both_protocols(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_safe_ca", lambda _path: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--host",
            "10.200.0.1",
            "--ca-file",
            "/root/root.crt",
            "--tls-port",
            "8443",
        ],
    )
    assert module.main() == 1
