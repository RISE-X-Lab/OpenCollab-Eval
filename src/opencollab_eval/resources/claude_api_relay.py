#!/usr/bin/env python3
"""Minimal streaming HTTP relay for an isolated Claude runtime network."""

from __future__ import annotations

import http.client
import http.server
import os
import socket
import urllib.error
import urllib.request

MAX_REQUEST_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024 * 1024
UPSTREAM = os.environ.get("CLAUDE_RELAY_UPSTREAM", "").rstrip("/")
UPSTREAM_UNIX = os.environ.get("CLAUDE_RELAY_UPSTREAM_UNIX", "")
if bool(UPSTREAM) == bool(UPSTREAM_UNIX):
    raise RuntimeError("configure exactly one Claude relay upstream")


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
        if self.path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
        body = self.rfile.read(length)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"connection", "content-length", "host"}
        }
        connection = None
        if UPSTREAM_UNIX:
            connection = UnixHTTPConnection(UPSTREAM_UNIX, timeout=300)
            connection.request(self.command, self.path, body=body, headers=headers)
            upstream = connection.getresponse()
        else:
            request = urllib.request.Request(
                UPSTREAM + self.path,
                data=body if self.command != "GET" else None,
                headers=headers,
                method=self.command,
            )
            try:
                upstream = urllib.request.urlopen(request, timeout=300)
            except urllib.error.HTTPError as exc:
                upstream = exc
        try:
            self.send_response(upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            total = 0
            reader = getattr(upstream, "read1", upstream.read)
            while True:
                chunk = reader(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            upstream.close()
            if connection is not None:
                connection.close()

    do_GET = _relay
    do_POST = _relay


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Relay).serve_forever()
