from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "caddy_readiness.py"


def _module():
    spec = importlib.util.spec_from_file_location("caddy_readiness_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
