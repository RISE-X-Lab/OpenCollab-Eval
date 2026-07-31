"""Loopback-only authenticated relay for OpenAI-compatible model APIs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import http.client
import json
import math
import queue
import select
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opencollab_eval.commands.llm_api_stream import (
    ChatStreamError,
    aggregate_chat_stream,
    streaming_chat_request,
)
from opencollab_eval.commands.swe_v1_prolite_config import load_shell_env

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RECORDED_REQUEST_SHAPES = 2048
UPSTREAM_OPEN_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class ProxyConfig:
    client_token: str
    upstream_api_key: str
    upstream_base_url: str
    timeout: float
    aggregate_chat_stream: bool = False
    compact_tool_schemas: bool = False
    gzip_upstream_request: bool = False
    max_upstream_request_bytes: int = 0
    allow_insecure_upstream: bool = False
    direct_upstream: bool = False


def _required(values: dict[str, str], *names: str) -> str:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing proxy configuration: " + " or ".join(names))


def load_proxy_config(
    env_file: Path,
    *,
    upstream_base_url: str = "",
    timeout: float = 900.0,
    allow_insecure_upstream: bool = False,
) -> ProxyConfig:
    mode = env_file.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise PermissionError("proxy environment file must be a private regular file")
    values = load_shell_env(env_file)
    base_url = upstream_base_url.strip() or _required(values, "OPENCOLLAB_UPSTREAM_BASE_URL")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme not in ({"https", "http"} if allow_insecure_upstream else {"https"})
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        expected = "an HTTP or HTTPS" if allow_insecure_upstream else "an HTTPS"
        raise ValueError(f"upstream base URL must be {expected} origin without credentials or query data")
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("upstream timeout must be a positive finite number")
    return ProxyConfig(
        client_token=_required(values, "OPENCOLLAB_PROXY_CLIENT_TOKEN"),
        upstream_api_key=_required(
            values,
            "OPENCOLLAB_UPSTREAM_API_KEY",
            "KIMI_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        ),
        upstream_base_url=base_url.rstrip("/"),
        timeout=timeout,
        allow_insecure_upstream=allow_insecure_upstream,
    )


def upstream_base_url_sha256(config: ProxyConfig) -> str:
    return hashlib.sha256(config.upstream_base_url.encode()).hexdigest()


def _upstream_url(base_url: str, request_path: str) -> str:
    path = urllib.parse.urlsplit(request_path).path
    if path == "/v1/chat/completions" and base_url.endswith("/v1"):
        path = "/chat/completions"
    elif path == "/v1/messages" and base_url.endswith("/v1"):
        path = "/messages"
    elif path == "/v1/responses" and base_url.endswith("/v1"):
        path = "/responses"
    if path not in {
        "/chat/completions",
        "/v1/chat/completions",
        "/v1/messages",
        "/messages",
        "/responses",
        "/v1/responses",
    }:
        raise ValueError("unsupported model API path")
    return base_url + path


def _codex_responses_request(body: bytes) -> tuple[bytes, dict[str, str]]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Responses request must be a JSON object")
    payload.setdefault("include", ["reasoning.encrypted_content"])
    payload.setdefault("parallel_tool_calls", False)
    payload.setdefault("text", {"verbosity": "low"})
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning.setdefault("context", "all_turns")
    if not payload.get("prompt_cache_key"):
        input_value = payload.get("input")
        root_input = input_value[0] if isinstance(input_value, list) and input_value else input_value
        seed = json.dumps(
            {
                "instructions": payload.get("instructions"),
                "model": payload.get("model"),
                "root_input": root_input,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        payload["prompt_cache_key"] = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(payload["prompt_cache_key"])))
    turn_seed = json.dumps(payload.get("input"), sort_keys=True, separators=(",", ":"))
    turn_id = str(uuid.uuid5(uuid.UUID(session_id), turn_seed))
    installation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "opencollab-eval"))
    window_id = f"{session_id}:0"
    turn_metadata = {
        "installation_id": installation_id,
        "request_kind": "turn",
        "session_id": session_id,
        "thread_id": session_id,
        "thread_source": "user",
        "turn_id": turn_id,
        "window_id": window_id,
    }
    client_metadata = payload.setdefault("client_metadata", {})
    if isinstance(client_metadata, dict):
        for name, value in {
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": turn_id,
            "x-codex-installation-id": installation_id,
            "x-codex-turn-metadata": json.dumps(turn_metadata, separators=(",", ":")),
            "x-codex-window-id": window_id,
        }.items():
            client_metadata.setdefault(name, value)
    headers = {
        "session-id": session_id,
        "thread-id": session_id,
        "x-client-request-id": str(uuid.uuid4()),
        "x-codex-beta-features": "remote_compaction_v2",
        "x-codex-turn-metadata": json.dumps(turn_metadata, separators=(",", ":")),
        "x-codex-window-id": window_id,
    }
    return json.dumps(payload, separators=(",", ":")).encode(), headers


class _ClientDisconnected(ConnectionError):
    pass


def _diagnostic(event: str, **fields: object) -> None:
    details = " ".join(f"{name}={value}" for name, value in sorted(fields.items()))
    print(f"model_relay {event} {details}".rstrip(), file=sys.stderr, flush=True)


def _responses_request_shape(body: bytes, user_agent: str) -> dict[str, object]:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Responses request must be a JSON object")
    input_items = payload.get("input")
    tools = payload.get("tools")
    reasoning = payload.get("reasoning")
    return {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "fields": ",".join(sorted(str(key) for key in payload)),
        "input_items": len(input_items) if isinstance(input_items, list) else 1,
        "reasoning_effort": (
            reasoning.get("effort", "") if isinstance(reasoning, dict) else ""
        ),
        "request_bytes": len(body),
        "tools": len(tools) if isinstance(tools, list) else 0,
        "user_agent_sha256": hashlib.sha256(user_agent.encode()).hexdigest(),
    }


class _DirectResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
    ) -> None:
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = response.headers

    def __enter__(self) -> _DirectResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._response.close()
        self._connection.close()

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def read1(self, size: int = -1) -> bytes:
        return self._response.read1(size)

    def readline(self, size: int = -1) -> bytes:
        return self._response.readline(size)


def _client_disconnected(client: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([client], [], [], 0)
        return bool(readable) and not client.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _abort_connection(connection: http.client.HTTPConnection) -> None:
    upstream_socket = connection.sock
    if upstream_socket is not None:
        try:
            upstream_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


def _open_direct_upstream(
    request: urllib.request.Request,
    client: socket.socket,
    timeout: float,
) -> _DirectResponse:
    parsed = urllib.parse.urlsplit(request.full_url)
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    result: queue.Queue[tuple[_DirectResponse | None, BaseException | None]] = queue.Queue()

    def open_response() -> None:
        try:
            connection.request(
                request.get_method(),
                target,
                body=request.data,
                headers=dict(request.header_items()),
            )
            result.put((_DirectResponse(connection.getresponse(), connection), None))
        except BaseException as exc:
            result.put((None, exc))

    worker = threading.Thread(target=open_response, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or _client_disconnected(client):
            _abort_connection(connection)
            worker.join(1)
            if remaining <= 0:
                raise TimeoutError("upstream response headers timed out")
            raise _ClientDisconnected("downstream client disconnected")
        try:
            response, error = result.get(
                timeout=min(UPSTREAM_OPEN_POLL_SECONDS, remaining)
            )
        except queue.Empty:
            continue
        if error is not None:
            raise error
        assert response is not None
        return response


def make_handler(config: ProxyConfig) -> type[BaseHTTPRequestHandler]:
    seen_request_shapes: set[str] = set()
    seen_request_shapes_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenCollabEvalProxy/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(
            self,
            status: int,
            payload: dict[str, object],
            *,
            retry_after: int | None = None,
        ) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry_after is not None:
                self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path != "/healthz":
                self._json(404, {"error": "not_found"})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "kind": "authenticated_model_relay",
                    "aggregate_chat_stream": config.aggregate_chat_stream,
                    "compact_tool_schemas": config.compact_tool_schemas,
                    "gzip_upstream_request": config.gzip_upstream_request,
                    "responses_passthrough": True,
                    "allow_insecure_upstream": config.allow_insecure_upstream,
                    "direct_upstream": config.direct_upstream,
                    "max_upstream_request_bytes": config.max_upstream_request_bytes,
                    "upstream_timeout": config.timeout,
                    "upstream_base_url_sha256": upstream_base_url_sha256(config),
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            supplied = supplied or self.headers.get("x-api-key", "").strip()
            if not supplied or not hmac.compare_digest(supplied, config.client_token):
                self._json(401, {"error": "invalid_client_token"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._json(413, {"error": "invalid_request_size"})
                return
            body = self.rfile.read(length)
            aggregate_stream = False
            expected_model = ""
            codex_headers: dict[str, str] = {}
            try:
                request_path = urllib.parse.urlsplit(self.path).path
                url = _upstream_url(config.upstream_base_url, self.path)
                if (
                    request_path in {"/responses", "/v1/responses"}
                    and not config.direct_upstream
                ):
                    self._json(400, {"error": "responses_require_cancellable_transport"})
                    return
                user_agent = self.headers.get("User-Agent", "opencollab-eval")
                codex_compatible = user_agent.startswith("codex_cli_rs/")
                if codex_compatible and request_path in {"/responses", "/v1/responses"}:
                    body, codex_headers = _codex_responses_request(body)
                    shape = _responses_request_shape(body, user_agent)
                    body_sha256 = str(shape["body_sha256"])
                    with seen_request_shapes_lock:
                        first_observation = body_sha256 not in seen_request_shapes
                        if first_observation:
                            if len(seen_request_shapes) >= MAX_RECORDED_REQUEST_SHAPES:
                                seen_request_shapes.clear()
                            seen_request_shapes.add(body_sha256)
                    if first_observation:
                        _diagnostic("request_shape", **shape)
                if (
                    config.aggregate_chat_stream or config.compact_tool_schemas
                ) and request_path in {
                    "/chat/completions",
                    "/v1/chat/completions",
                }:
                    try:
                        body, aggregate_stream, expected_model = streaming_chat_request(
                            body,
                            compact_tool_schemas=config.compact_tool_schemas,
                            enable_stream=config.aggregate_chat_stream,
                        )
                    except ChatStreamError:
                        self._json(400, {"error": "invalid_chat_request"})
                        return
                if (
                    config.max_upstream_request_bytes
                    and len(body) > config.max_upstream_request_bytes
                ):
                    self._json(
                        400,
                        {
                            "error": {
                                "message": "request exceeds the configured context window byte limit",
                                "type": "invalid_request_error",
                                "code": "context_length_exceeded",
                            }
                        },
                    )
                    return
                upstream_body = (
                    gzip.compress(body, mtime=0)
                    if config.gzip_upstream_request
                    else body
                )
                headers = {
                    "Authorization": f"Bearer {config.upstream_api_key}",
                    "x-api-key": config.upstream_api_key,
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "Accept": self.headers.get("Accept", "application/json"),
                    "User-Agent": user_agent,
                }
                if config.gzip_upstream_request:
                    headers["Content-Encoding"] = "gzip"
                headers.update(codex_headers)
                originator = self.headers.get("originator", "").strip()
                if not originator and codex_compatible:
                    originator = "Codex Desktop"
                if originator:
                    headers["originator"] = originator
                responses_lite = self.headers.get(
                    "x-openai-internal-codex-responses-lite", ""
                ).strip()
                if responses_lite:
                    headers["x-openai-internal-codex-responses-lite"] = responses_lite
                elif codex_compatible:
                    headers["x-openai-internal-codex-responses-lite"] = "true"
                for name in ("anthropic-version", "anthropic-beta"):
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                request = urllib.request.Request(
                    url, data=upstream_body, headers=headers, method="POST"
                )
                upstream_started = time.monotonic()
                try:
                    response = (
                        _open_direct_upstream(request, self.connection, config.timeout)
                        if config.direct_upstream
                        else urllib.request.urlopen(request, timeout=config.timeout)
                    )
                except urllib.error.HTTPError as exc:
                    response = exc
                except _ClientDisconnected:
                    _diagnostic(
                        "client_disconnected",
                        latency_s=f"{time.monotonic() - upstream_started:.3f}",
                        path=request_path,
                    )
                    return
                _diagnostic(
                    "upstream_response",
                    latency_s=f"{time.monotonic() - upstream_started:.3f}",
                    path=request_path,
                    request_bytes=len(body),
                    wire_request_bytes=len(upstream_body),
                    status=int(response.status),
                )
            except (OSError, ValueError, urllib.error.URLError):
                _diagnostic(
                    "upstream_error",
                    error=sys.exc_info()[0].__name__,
                    latency_s=(
                        f"{time.monotonic() - upstream_started:.3f}"
                        if "upstream_started" in locals()
                        else "0.000"
                    ),
                    path=urllib.parse.urlsplit(self.path).path,
                    request_bytes=len(body) if "body" in locals() else 0,
                )
                self._json(502, {"error": "upstream_request_failed"})
                return
            with response:
                if aggregate_stream and int(response.status) < 400:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        self._json(502, {"error": "invalid_upstream_stream"})
                        return
                    try:
                        payload = aggregate_chat_stream(
                            response,
                            byte_limit=MAX_RESPONSE_BYTES,
                            timeout=config.timeout,
                            expected_model=expected_model,
                        )
                    except (ChatStreamError, OSError) as exc:
                        _diagnostic(
                            "invalid_upstream_stream",
                            error=type(exc).__name__,
                            reason=str(exc).replace(" ", "_"),
                        )
                        self._json(502, {"error": "invalid_upstream_stream"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    request_id = response.headers.get("x-request-id", "")
                    if request_id:
                        self.send_header("x-request-id", request_id)
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after:
                        self.send_header("Retry-After", retry_after)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(payload)
                    return
                self.send_response(int(response.status))
                self.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type", "application/json"),
                )
                request_id = response.headers.get("x-request-id", "")
                if request_id:
                    self.send_header("x-request-id", request_id)
                retry_after = response.headers.get("Retry-After", "")
                if retry_after:
                    self.send_header("Retry-After", retry_after)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                total = 0
                read_chunk = getattr(response, "read1", response.read)
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a loopback-only authenticated model API relay")
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--upstream-base-url", default="")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--aggregate-chat-stream", action="store_true")
    parser.add_argument("--compact-tool-schemas", action="store_true")
    parser.add_argument("--gzip-upstream-request", action="store_true")
    parser.add_argument("--max-upstream-request-bytes", type=int, default=0)
    parser.add_argument("--allow-insecure-upstream", action="store_true")
    parser.add_argument("--direct-upstream", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("proxy host must be loopback")
    config = load_proxy_config(
        args.env_file,
        upstream_base_url=args.upstream_base_url,
        timeout=args.timeout,
        allow_insecure_upstream=args.allow_insecure_upstream,
    )
    config = replace(
        config,
        aggregate_chat_stream=args.aggregate_chat_stream,
        compact_tool_schemas=args.compact_tool_schemas,
        gzip_upstream_request=args.gzip_upstream_request,
        max_upstream_request_bytes=max(0, args.max_upstream_request_bytes),
        direct_upstream=args.direct_upstream,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProxyConfig",
    "build_parser",
    "load_proxy_config",
    "main",
    "make_handler",
    "upstream_base_url_sha256",
]
