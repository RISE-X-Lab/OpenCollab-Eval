"""Managed relay arguments and health checks for the unified SWE entrypoint."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import urllib.error
import urllib.request

from .swe_v1_prolite_config import url_with_healthz


def relay_mode_flags(
    mode: str,
    *,
    compact_tool_schemas: bool,
    gzip_upstream_request: bool = False,
    max_upstream_request_bytes: int,
    allow_insecure_upstream: bool = False,
    direct_upstream: bool = False,
) -> list[str]:
    if mode not in {
        "aggregate-chat-stream",
        "responses-pass-through",
    }:
        raise ValueError(f"unsupported relay mode: {mode}")
    if max_upstream_request_bytes < 0:
        raise ValueError("proxy request byte limit must be non-negative")
    flags = []
    if mode == "aggregate-chat-stream":
        flags.append("--aggregate-chat-stream")
    if compact_tool_schemas:
        flags.append("--compact-tool-schemas")
    if gzip_upstream_request:
        flags.append("--gzip-upstream-request")
    if max_upstream_request_bytes:
        flags += ["--max-upstream-request-bytes", str(max_upstream_request_bytes)]
    if allow_insecure_upstream:
        flags.append("--allow-insecure-upstream")
    if direct_upstream:
        flags.append("--direct-upstream")
    return flags


def remote_proxy_healthy(
    *,
    ssh_command: str,
    host: str,
    base_url: str,
    upstream_base_url: str,
    relay_mode: str = "aggregate-chat-stream",
    compact_tool_schemas: bool = False,
    gzip_upstream_request: bool = False,
    max_upstream_request_bytes: int = 0,
    allow_insecure_upstream: bool = False,
    direct_upstream: bool = False,
    upstream_timeout: float = 900.0,
) -> bool:
    expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
    probe = (
        "import json,sys,urllib.request;"
        "v=json.load(urllib.request.urlopen(sys.argv[1],timeout=5));"
        "raise SystemExit(0 if v.get('kind')=='authenticated_model_relay' "
        "and v.get('aggregate_chat_stream') is (sys.argv[3]=='aggregate-chat-stream') "
        "and v.get('responses_passthrough') is True "
        "and v.get('allow_insecure_upstream') is (sys.argv[6]=='1') "
        "and v.get('direct_upstream') is (sys.argv[8]=='1') "
        "and v.get('compact_tool_schemas') is (sys.argv[4]=='1') "
        "and v.get('gzip_upstream_request') is (sys.argv[9]=='1') "
        "and v.get('max_upstream_request_bytes')==int(sys.argv[5]) "
        "and v.get('upstream_timeout')==float(sys.argv[7]) "
        "and v.get('upstream_base_url_sha256')==sys.argv[2] else 3)"
    )
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        shlex.join(
            [
                "python3",
                "-c",
                probe,
                url_with_healthz(base_url),
                expected,
                relay_mode,
                str(int(compact_tool_schemas)),
                str(max_upstream_request_bytes),
                str(int(allow_insecure_upstream)),
                str(upstream_timeout),
                str(int(direct_upstream)),
                str(int(gzip_upstream_request)),
            ]
        ),
    ]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def remote_proxy_socket_healthy(
    *,
    ssh_command: str,
    host: str,
    socket_path: str,
    upstream_base_url: str,
    relay_mode: str = "aggregate-chat-stream",
    compact_tool_schemas: bool = False,
    gzip_upstream_request: bool = False,
    max_upstream_request_bytes: int = 0,
    allow_insecure_upstream: bool = False,
    direct_upstream: bool = False,
    upstream_timeout: float = 900.0,
) -> bool:
    expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
    probe = "\n".join(
        (
            "import http.client,json,os,socket,stat,sys",
            "path,expected,relay_mode,compact,max_bytes,allow_insecure,timeout,direct,gzip_request=sys.argv[1:10]",
            "mode=os.stat(path).st_mode",
            "assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) & 0o077 == 0",
            "client=socket.socket(socket.AF_UNIX)",
            "client.settimeout(5)",
            "client.connect(path)",
            'client.sendall(b"GET /healthz HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n")',
            "stream=client.makefile('rb')",
            "status=stream.readline().split()",
            "assert len(status) >= 2 and status[1] == b'200'",
            "headers=http.client.parse_headers(stream)",
            "length=int(headers['Content-Length'])",
            "assert 0 <= length <= 65536",
            "body=stream.read(length)",
            "assert len(body) == length",
            "value=json.loads(body)",
            "assert value.get('kind') == 'authenticated_model_relay'",
            "assert value.get('aggregate_chat_stream') is (relay_mode == 'aggregate-chat-stream')",
            "assert value.get('responses_passthrough') is True",
            "assert value.get('allow_insecure_upstream') is (allow_insecure == '1')",
            "assert value.get('direct_upstream') is (direct == '1')",
            "assert value.get('compact_tool_schemas') is (compact == '1')",
            "assert value.get('gzip_upstream_request') is (gzip_request == '1')",
            "assert value.get('max_upstream_request_bytes') == int(max_bytes)",
            "assert value.get('upstream_timeout') == float(timeout)",
            "assert value.get('upstream_base_url_sha256') == expected",
        )
    )
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        shlex.join(
            [
                "python3",
                "-c",
                probe,
                socket_path,
                expected,
                relay_mode,
                str(int(compact_tool_schemas)),
                str(max_upstream_request_bytes),
                str(int(allow_insecure_upstream)),
                str(upstream_timeout),
                str(int(direct_upstream)),
                str(int(gzip_upstream_request)),
            ]
        ),
    ]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def local_relay_healthy(
    base_url: str,
    upstream_base_url: str,
    *,
    relay_mode: str = "aggregate-chat-stream",
    compact_tool_schemas: bool = False,
    gzip_upstream_request: bool = False,
    max_upstream_request_bytes: int = 0,
    allow_insecure_upstream: bool = False,
    direct_upstream: bool = False,
    upstream_timeout: float = 900.0,
) -> bool:
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=5) as response:
            payload = json.load(response)
        expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
        return (
            payload.get("kind") == "authenticated_model_relay"
            and payload.get("aggregate_chat_stream")
            is (relay_mode == "aggregate-chat-stream")
            and payload.get("responses_passthrough") is True
            and payload.get("allow_insecure_upstream") is allow_insecure_upstream
            and payload.get("direct_upstream") is direct_upstream
            and payload.get("compact_tool_schemas") is compact_tool_schemas
            and payload.get("gzip_upstream_request") is gzip_upstream_request
            and payload.get("max_upstream_request_bytes")
            == max_upstream_request_bytes
            and payload.get("upstream_timeout") == upstream_timeout
            and payload.get("upstream_base_url_sha256") == expected
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


__all__ = [
    "local_relay_healthy",
    "relay_mode_flags",
    "remote_proxy_healthy",
    "remote_proxy_socket_healthy",
]
