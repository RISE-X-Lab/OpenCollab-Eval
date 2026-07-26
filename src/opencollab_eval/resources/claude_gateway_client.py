#!/usr/bin/env python3
"""Bounded client for the task-bound Claude command gateway."""

from __future__ import annotations

import base64
import json
import socket
import sys

MAX_RESPONSE_BYTES = 128 * 1024 * 1024

endpoint = sys.argv[1]
if ":" in endpoint:
    host, port = endpoint.rsplit(":", 1)
    client = socket.create_connection((host, int(port)), timeout=1800)
else:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1800)
    client.connect(endpoint)
client.sendall(json.dumps(sys.argv[2:], separators=(",", ":")).encode() + b"\n")
response = bytearray()
while b"\n" not in response:
    chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
    if not chunk or len(response) + len(chunk) > MAX_RESPONSE_BYTES:
        raise SystemExit("invalid command gateway response")
    response.extend(chunk)
payload = json.loads(bytes(response).split(b"\n", 1)[0])
decode = base64.b64decode if payload.get("encoding") == "base64" else str.encode
sys.stdout.buffer.write(decode(payload.get("stdout", "")))
sys.stderr.buffer.write(decode(payload.get("stderr", "")))
raise SystemExit(payload.get("returncode", 125))
