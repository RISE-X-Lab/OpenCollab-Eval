"""Managed relay arguments and health checks for the unified SWE entrypoint."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import Any

from .swe_v1_prolite_config import url_with_healthz

RELAY_COMPACT_IDS_ENV = "OPENCOLLAB_RELAY_COMPACT_TOOL_CALL_IDS"


def add_relay_arguments(parser: Any) -> None:
    parser.add_argument(
        "--proxy-mode",
        choices=("aggregate-chat-stream", "responses-pass-through"),
        default="aggregate-chat-stream",
        help="Compatibility mode required from the managed model relay",
    )
    parser.add_argument("--proxy-compact-tool-schemas", action="store_true",
                        help="Remove non-semantic tool schema annotations")
    parser.add_argument("--proxy-compact-tool-call-ids", action="store_true", help="Shorten historical tool-call IDs")
    parser.add_argument("--proxy-gzip-upstream-request", action="store_true",
                        help="Use deterministic gzip for provider requests")
    parser.add_argument("--proxy-max-upstream-request-bytes", type=int, default=0,
                        help="Bound compatibility-relay requests after deterministic compaction")
    parser.add_argument("--proxy-allow-insecure-upstream", action="store_true",
                        help="Explicitly allow an HTTP provider URL for the managed relay")
    parser.add_argument("--proxy-direct-upstream", action="store_true",
                        help="Bypass host proxy settings for the managed relay upstream")


def relay_identity_arguments(enabled: bool, workflow_env: list[str]) -> list[str]:
    if any(value.partition("=")[0] == RELAY_COMPACT_IDS_ENV for value in workflow_env):
        raise SystemExit(
            f"{RELAY_COMPACT_IDS_ENV} is managed by --proxy-compact-tool-call-ids"
        )
    return ["--workflow-env", f"{RELAY_COMPACT_IDS_ENV}=true"] if enabled else []


def relay_runtime_options(args: Any) -> dict[str, Any]:
    return {
        "relay_mode": args.proxy_mode,
        "compact_tool_schemas": args.proxy_compact_tool_schemas,
        "compact_tool_call_ids": args.proxy_compact_tool_call_ids,
        "gzip_upstream_request": args.proxy_gzip_upstream_request,
        "max_upstream_request_bytes": args.proxy_max_upstream_request_bytes,
        "allow_insecure_upstream": args.proxy_allow_insecure_upstream,
        "direct_upstream": args.proxy_direct_upstream,
    }


def relay_mode_flags(
    mode: str,
    *,
    compact_tool_schemas: bool,
    compact_tool_call_ids: bool = False,
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
    if compact_tool_call_ids:
        flags.append("--compact-tool-call-ids")
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
    compact_tool_call_ids: bool = False,
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
        "and v.get('compact_tool_call_ids') is (sys.argv[10]=='1') "
        "and v.get('gzip_upstream_request') is (sys.argv[9]=='1') "
        "and v.get('max_upstream_request_bytes')==int(sys.argv[5]) "
        "and (int(sys.argv[5])==0 or v.get('upstream_request_limit_basis')=='wire_bytes') "
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
                str(int(compact_tool_call_ids)),
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
    compact_tool_call_ids: bool = False,
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
            "path,expected,relay_mode,compact,max_bytes,allow_insecure,timeout,direct,gzip_request,compact_ids=sys.argv[1:11]",
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
            "assert value.get('compact_tool_call_ids') is (compact_ids == '1')",
            "assert value.get('gzip_upstream_request') is (gzip_request == '1')",
            "assert value.get('max_upstream_request_bytes') == int(max_bytes)",
            "assert int(max_bytes) == 0 or value.get('upstream_request_limit_basis') == 'wire_bytes'",
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
                str(int(compact_tool_call_ids)),
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
    compact_tool_call_ids: bool = False,
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
            and payload.get("compact_tool_call_ids") is compact_tool_call_ids
            and payload.get("gzip_upstream_request") is gzip_upstream_request
            and payload.get("max_upstream_request_bytes")
            == max_upstream_request_bytes
            and (
                max_upstream_request_bytes == 0
                or payload.get("upstream_request_limit_basis") == "wire_bytes"
            )
            and payload.get("upstream_timeout") == upstream_timeout
            and payload.get("upstream_base_url_sha256") == expected
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


__all__ = [
    "add_relay_arguments",
    "local_relay_healthy",
    "relay_identity_arguments",
    "relay_mode_flags",
    "relay_runtime_options",
    "remote_proxy_healthy",
    "remote_proxy_socket_healthy",
]
