#!/usr/bin/env python3
"""Minimal streaming HTTP relay for an isolated Claude runtime network."""

from __future__ import annotations

import http.client
import http.server
import math
import os
import socket
import time
import urllib.error
import urllib.request

MAX_REQUEST_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024 * 1024
# Keep the sidecar's socket deadline finite even when a caller controls its
# environment.  The host runner derives a relay deadline as
# ``min(llm_timeout, max(activity_timeouts)) + 60``; 21,600 seconds is the
# largest supported model timeout in the current runner contract.
# Preserve the standalone relay's historical bound when no host contract is
# supplied.  The launcher always passes its derived (usually 240s) value.
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300.0
MAX_UPSTREAM_TIMEOUT_SECONDS = 6 * 60 * 60 + 60.0
UPSTREAM = os.environ.get("CLAUDE_RELAY_UPSTREAM", "").rstrip("/")
UPSTREAM_UNIX = os.environ.get("CLAUDE_RELAY_UPSTREAM_UNIX", "")
if bool(UPSTREAM) == bool(UPSTREAM_UNIX):
    raise RuntimeError("configure exactly one Claude relay upstream")


def _configured_upstream_timeout() -> float:
    raw = os.environ.get("CLAUDE_RELAY_UPSTREAM_TIMEOUT", "")
    if not raw:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(
            "CLAUDE_RELAY_UPSTREAM_TIMEOUT must be finite, positive, and bounded"
        ) from exc
    if not math.isfinite(value) or not 0 < value <= MAX_UPSTREAM_TIMEOUT_SECONDS:
        raise RuntimeError(
            "CLAUDE_RELAY_UPSTREAM_TIMEOUT must be finite, positive, and bounded"
        )
    return value


UPSTREAM_TIMEOUT = _configured_upstream_timeout()


class RelayDeadlineExceeded(TimeoutError):
    """The relay's single request budget was exhausted."""


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not math.isfinite(remaining):
        raise RelayDeadlineExceeded("Claude relay request deadline exceeded")
    return remaining


def _socket_like(value: object) -> object | None:
    """Find a socket exposed by a file/HTTP response wrapper.

    ``BaseHTTPRequestHandler`` and urllib/http.client deliberately expose
    different wrapper layers.  Walking the small, known wrapper chain keeps
    timeout enforcement compatible with both transports and with lightweight
    test doubles without depending on private implementation details beyond
    the conventional ``fp.raw._sock`` path.
    """

    current = value
    seen: set[int] = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if callable(getattr(current, "settimeout", None)):
            return current
        next_value = None
        for name in ("_sock", "sock", "socket", "raw", "fp", "_fileobject"):
            try:
                candidate = getattr(current, name, None)
            except (AttributeError, OSError):
                candidate = None
            if candidate is not None and id(candidate) not in seen:
                next_value = candidate
                break
        current = next_value
    return None


def _set_stream_timeout(value: object, deadline: float) -> float:
    """Set a per-I/O timeout to the remaining end-to-end budget."""

    remaining = _remaining(deadline)
    stream_socket = _socket_like(value)
    if stream_socket is not None:
        stream_socket.settimeout(remaining)
    return remaining


def _read_request_body(handler: Relay, length: int, deadline: float) -> bytes:
    """Read exactly ``length`` bytes while honoring the request deadline."""

    if length == 0:
        return b""
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        _set_stream_timeout(getattr(handler, "connection", None), deadline)
        chunk = handler.rfile.read(min(64 * 1024, remaining))
        if not chunk:
            raise RelayDeadlineExceeded(
                "client disconnected before the declared request body completed"
            )
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("request body reader returned a non-byte value")
        chunk = bytes(chunk)
        if len(chunk) > remaining:
            raise ValueError("request body exceeded the declared Content-Length")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _relay(self) -> None:
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        # Arm the client socket before any error response or request-body
        # parsing.  ``send_error`` can itself write to a stalled client.
        _set_stream_timeout(self.wfile, deadline)
        if self.path == "/health":
            body = b"ok\n"
            _set_stream_timeout(self.wfile, deadline)
            self.send_response(200)
            _set_stream_timeout(self.wfile, deadline)
            self.send_header("Content-Length", str(len(body)))
            _set_stream_timeout(self.wfile, deadline)
            self.end_headers()
            _set_stream_timeout(self.wfile, deadline)
            self.wfile.write(body)
            _set_stream_timeout(self.wfile, deadline)
            self.wfile.flush()
            return
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            self.send_error(400)
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.send_error(413)
            return
        body = _read_request_body(self, length, deadline)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"connection", "content-length", "host"}
        }
        connection = None
        upstream = None
        try:
            if UPSTREAM_UNIX:
                connection = UnixHTTPConnection(
                    UPSTREAM_UNIX, timeout=_remaining(deadline)
                )
                # ``HTTPConnection.request`` may perform the connect itself;
                # refresh the timeout immediately before it starts so a slow
                # inbound body cannot donate its already-spent budget.
                connection.timeout = _remaining(deadline)
                connection.request(self.command, self.path, body=body, headers=headers)
                _set_stream_timeout(getattr(connection, "sock", None), deadline)
                upstream = connection.getresponse()
            else:
                request = urllib.request.Request(
                    UPSTREAM + self.path,
                    data=body if self.command != "GET" else None,
                    headers=headers,
                    method=self.command,
                )
                try:
                    upstream = urllib.request.urlopen(
                        request, timeout=_remaining(deadline)
                    )
                except urllib.error.HTTPError as exc:
                    upstream = exc
            if upstream is None:
                raise RuntimeError("Claude relay upstream did not return a response")
            _set_stream_timeout(upstream, deadline)
            _set_stream_timeout(self.wfile, deadline)
            # ``urllib.error.HTTPError`` is also a response object, but it
            # exposes the status as ``code`` rather than ``status``.  Treating
            # an upstream 4xx/5xx as an attribute error used to turn a valid
            # provider response into an unstructured relay failure.
            upstream_status = getattr(upstream, "status", None)
            if upstream_status is None:
                upstream_status = getattr(upstream, "code", None)
            if not isinstance(upstream_status, int):
                raise RuntimeError("Claude relay upstream returned no HTTP status")
            self.send_response(upstream_status)
            for key, value in upstream.headers.items():
                if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                    _set_stream_timeout(self.wfile, deadline)
                    self.send_header(key, value)
            _set_stream_timeout(self.wfile, deadline)
            self.send_header("Connection", "close")
            _set_stream_timeout(self.wfile, deadline)
            self.end_headers()
            total = 0
            reader = getattr(upstream, "read1", None) or upstream.read
            while True:
                _set_stream_timeout(upstream, deadline)
                chunk = reader(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    self.close_connection = True
                    return
                _set_stream_timeout(self.wfile, deadline)
                self.wfile.write(chunk)
                _set_stream_timeout(self.wfile, deadline)
                self.wfile.flush()
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    do_GET = _relay
    do_POST = _relay


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Relay).serve_forever()
