from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class Http3Response:
    status: int
    headers: dict[str, str]
    body: str
    time_to_first_byte_seconds: float | None


async def _request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    ca_file: Path | None,
) -> Http3Response:
    try:
        from aioquic.asyncio import QuicConnectionProtocol, connect
        from aioquic.h3.connection import H3_ALPN, H3Connection
        from aioquic.h3.events import DataReceived, HeadersReceived
        from aioquic.quic.configuration import QuicConfiguration
    except ImportError as exc:
        raise RuntimeError(
            "HTTP/3 requires aioquic; install traffic_experiment/requirements-runner.txt"
        ) from exc

    class Protocol(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.waiters: dict[int, asyncio.Future[Http3Response]] = {}
            self.response_headers: dict[int, list[tuple[bytes, bytes]]] = {}
            self.response_bodies: dict[int, bytearray] = {}
            self.started: dict[int, float] = {}
            self.first_byte: dict[int, float] = {}

        def quic_event_received(self, event) -> None:
            for http_event in self.http.handle_event(event):
                stream_id = getattr(http_event, "stream_id", None)
                if stream_id not in self.waiters:
                    continue
                if stream_id not in self.first_byte:
                    self.first_byte[stream_id] = time.perf_counter()
                if isinstance(http_event, HeadersReceived):
                    self.response_headers[stream_id].extend(http_event.headers)
                elif isinstance(http_event, DataReceived):
                    self.response_bodies[stream_id].extend(http_event.data)
                if getattr(http_event, "stream_ended", False):
                    raw_headers = self.response_headers.pop(stream_id)
                    decoded = {
                        key.decode("ascii"): value.decode("utf-8", errors="replace")
                        for key, value in raw_headers
                    }
                    status = int(decoded.pop(":status"))
                    first_byte = self.first_byte.pop(stream_id, None)
                    self.waiters.pop(stream_id).set_result(
                        Http3Response(
                            status=status,
                            headers=decoded,
                            body=self.response_bodies.pop(stream_id).decode(
                                "utf-8", errors="replace"
                            ),
                            time_to_first_byte_seconds=(
                                first_byte - self.started.pop(stream_id)
                                if first_byte is not None
                                else None
                            ),
                        )
                    )

        async def post(self) -> Http3Response:
            parsed = urlparse(url)
            stream_id = self._quic.get_next_available_stream_id()
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[Http3Response] = loop.create_future()
            self.waiters[stream_id] = waiter
            self.response_headers[stream_id] = []
            self.response_bodies[stream_id] = bytearray()
            self.started[stream_id] = time.perf_counter()
            authority = parsed.hostname or ""
            if parsed.port and parsed.port != 443:
                authority = f"{authority}:{parsed.port}"
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            request_headers = [
                (b":method", b"POST"),
                (b":scheme", b"https"),
                (b":authority", authority.encode()),
                (b":path", path.encode()),
                (b"content-type", b"application/json"),
            ]
            request_headers.extend(
                (name.lower().encode(), value.encode()) for name, value in headers.items()
            )
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.http.send_headers(stream_id, request_headers)
            self.http.send_data(stream_id, body, end_stream=True)
            self.transmit()
            return await asyncio.wait_for(waiter, timeout=timeout_seconds)

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("HTTP/3 endpoint must be an https URL with a hostname")
    configuration = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    configuration.verify_mode = ssl.CERT_REQUIRED
    if ca_file:
        configuration.load_verify_locations(str(ca_file))
    async with connect(
        parsed.hostname,
        parsed.port or 443,
        configuration=configuration,
        create_protocol=Protocol,
    ) as protocol:
        return await protocol.post()


def post_http3(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    ca_file: Path | None,
) -> Http3Response:
    return asyncio.run(_request(url, headers, payload, timeout_seconds, ca_file))
