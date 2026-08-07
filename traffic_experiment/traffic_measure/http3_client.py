from __future__ import annotations

import asyncio
import concurrent.futures
import json
import ssl
import threading
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
    response_body_bytes: int
    sse_event_count: int
    content_event_offsets_seconds: tuple[float, ...]


def _runtime():
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
            self.sse_buffers: dict[int, bytearray] = {}
            self.sse_event_counts: dict[int, int] = {}
            self.content_event_offsets: dict[int, list[float]] = {}

        def _consume_sse(self, stream_id: int, *, flush: bool = False) -> None:
            pending = bytes(self.sse_buffers[stream_id])
            events: list[bytes] = []
            while pending:
                separators = [
                    (index, separator)
                    for separator in (b"\n\n", b"\r\n\r\n")
                    if (index := pending.find(separator)) >= 0
                ]
                if not separators:
                    if flush:
                        events.append(pending)
                        pending = b""
                    break
                index, separator = min(separators, key=lambda item: item[0])
                events.append(pending[:index])
                pending = pending[index + len(separator) :]
            self.sse_buffers[stream_id] = bytearray(pending)
            for event in events:
                for line in event.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    self.sse_event_counts[stream_id] += 1
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        decoded = json.loads(payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if any(
                        (choice.get("delta") or {}).get("content")
                        for choice in decoded.get("choices", [])
                    ):
                        self.content_event_offsets[stream_id].append(
                            time.perf_counter() - self.started[stream_id]
                        )

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
                    self.sse_buffers[stream_id].extend(http_event.data)
                    self._consume_sse(stream_id)
                if getattr(http_event, "stream_ended", False):
                    self._consume_sse(stream_id, flush=True)
                    raw_headers = self.response_headers.pop(stream_id)
                    decoded = {
                        key.decode("ascii"): value.decode("utf-8", errors="replace")
                        for key, value in raw_headers
                    }
                    status = int(decoded.pop(":status"))
                    first_byte = self.first_byte.pop(stream_id, None)
                    started = self.started.pop(stream_id)
                    raw_body = self.response_bodies.pop(stream_id)
                    sse_event_count = self.sse_event_counts.pop(stream_id)
                    content_event_offsets = tuple(
                        self.content_event_offsets.pop(stream_id)
                    )
                    self.waiters.pop(stream_id).set_result(
                        Http3Response(
                            status=status,
                            headers=decoded,
                            body=raw_body.decode(
                                "utf-8", errors="replace"
                            ),
                            time_to_first_byte_seconds=(
                                first_byte - started
                                if first_byte is not None
                                else None
                            ),
                            response_body_bytes=len(raw_body),
                            sse_event_count=sse_event_count,
                            content_event_offsets_seconds=content_event_offsets,
                        )
                    )

        async def request(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
            payload: dict[str, Any] | None = None,
        ) -> Http3Response:
            parsed = urlparse(url)
            stream_id = self._quic.get_next_available_stream_id()
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[Http3Response] = loop.create_future()
            self.waiters[stream_id] = waiter
            self.response_headers[stream_id] = []
            self.response_bodies[stream_id] = bytearray()
            self.started[stream_id] = time.perf_counter()
            self.sse_buffers[stream_id] = bytearray()
            self.sse_event_counts[stream_id] = 0
            self.content_event_offsets[stream_id] = []
            authority = parsed.hostname or ""
            if parsed.port and parsed.port != 443:
                authority = f"{authority}:{parsed.port}"
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            request_headers = [
                (b":method", method.upper().encode()),
                (b":scheme", b"https"),
                (b":authority", authority.encode()),
                (b":path", path.encode()),
            ]
            if payload is not None:
                request_headers.append((b"content-type", b"application/json"))
            request_headers.extend(
                (name.lower().encode(), value.encode()) for name, value in headers.items()
            )
            self.http.send_headers(
                stream_id,
                request_headers,
                end_stream=payload is None,
            )
            if payload is not None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                self.http.send_data(stream_id, body, end_stream=True)
            self.transmit()
            try:
                return await asyncio.wait_for(waiter, timeout=timeout_seconds)
            finally:
                self.waiters.pop(stream_id, None)
                self.response_headers.pop(stream_id, None)
                self.response_bodies.pop(stream_id, None)
                self.started.pop(stream_id, None)
                self.first_byte.pop(stream_id, None)
                self.sse_buffers.pop(stream_id, None)
                self.sse_event_counts.pop(stream_id, None)
                self.content_event_offsets.pop(stream_id, None)

        async def post(
            self,
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
            timeout_seconds: float,
        ) -> Http3Response:
            return await self.request(
                "POST",
                url,
                headers,
                timeout_seconds,
                payload,
            )

        async def get(
            self,
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
        ) -> Http3Response:
            return await self.request("GET", url, headers, timeout_seconds)

    return connect, H3_ALPN, QuicConfiguration, Protocol


def _configuration(ca_file: Path | None):
    _, h3_alpn, configuration_type, _ = _runtime()
    configuration = configuration_type(is_client=True, alpn_protocols=h3_alpn)
    configuration.verify_mode = ssl.CERT_REQUIRED
    if ca_file:
        configuration.load_verify_locations(str(ca_file))
    return configuration


async def _request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    ca_file: Path | None,
) -> Http3Response:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("HTTP/3 endpoint must be an https URL with a hostname")
    connect, _, _, protocol_type = _runtime()
    configuration = _configuration(ca_file)
    async with connect(
        parsed.hostname,
        parsed.port or 443,
        configuration=configuration,
        create_protocol=protocol_type,
    ) as protocol:
        return await protocol.post(url, headers, payload, timeout_seconds)


class PersistentHttp3Client:
    """Synchronous facade over one reusable aioquic connection."""

    def __init__(
        self,
        base_url: str,
        ca_file: Path | None,
        connect_timeout_seconds: float = 30,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("HTTP/3 base URL must be https and include a hostname")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.ca_file = ca_file
        self.loop = asyncio.new_event_loop()
        self.ready: concurrent.futures.Future[bool] = concurrent.futures.Future()
        self.context: Any = None
        self.protocol: Any = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.result(timeout=connect_timeout_seconds)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._open())
        except BaseException as exc:
            self.ready.set_exception(exc)
            self.loop.close()
            return
        self.ready.set_result(True)
        self.loop.run_forever()
        self.loop.close()

    async def _open(self) -> None:
        connect, _, _, protocol_type = _runtime()
        self.context = connect(
            self.host,
            self.port,
            configuration=_configuration(self.ca_file),
            create_protocol=protocol_type,
        )
        self.protocol = await self.context.__aenter__()

    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> Http3Response:
        future = asyncio.run_coroutine_threadsafe(
            self.protocol.post(url, headers, payload, timeout_seconds),
            self.loop,
        )
        return future.result(timeout=timeout_seconds + 5)

    def get(
        self,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> Http3Response:
        future = asyncio.run_coroutine_threadsafe(
            self.protocol.get(url, headers, timeout_seconds),
            self.loop,
        )
        return future.result(timeout=timeout_seconds + 5)

    def close(self) -> None:
        if not self.thread.is_alive():
            return

        async def close_context() -> None:
            if self.context is not None:
                await self.context.__aexit__(None, None, None)

        future = asyncio.run_coroutine_threadsafe(close_context(), self.loop)
        future.result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


def post_http3(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    ca_file: Path | None,
) -> Http3Response:
    return asyncio.run(_request(url, headers, payload, timeout_seconds, ca_file))
