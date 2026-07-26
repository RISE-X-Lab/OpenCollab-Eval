"""Loopback-only authenticated relay for OpenAI-compatible model APIs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opencollab_eval.commands.swe_v1_prolite_config import load_shell_env

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ProxyConfig:
    client_token: str
    upstream_api_key: str
    upstream_base_url: str
    timeout: float


def _required(values: dict[str, str], *names: str) -> str:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    raise ValueError("missing proxy configuration: " + " or ".join(names))


def load_proxy_config(env_file: Path, *, upstream_base_url: str = "", timeout: float = 900.0) -> ProxyConfig:
    mode = env_file.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise PermissionError("proxy environment file must be a private regular file")
    values = load_shell_env(env_file)
    base_url = upstream_base_url.strip() or _required(values, "OPENCOLLAB_UPSTREAM_BASE_URL")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("upstream base URL must be an HTTPS origin without credentials or query data")
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
        timeout=max(1.0, float(timeout)),
    )


def upstream_base_url_sha256(config: ProxyConfig) -> str:
    return hashlib.sha256(config.upstream_base_url.encode()).hexdigest()


def _upstream_url(base_url: str, request_path: str) -> str:
    path = urllib.parse.urlsplit(request_path).path
    if path == "/v1/chat/completions" and base_url.endswith("/v1"):
        path = "/chat/completions"
    elif path == "/v1/messages" and base_url.endswith("/v1"):
        path = "/messages"
    if path not in {"/chat/completions", "/v1/chat/completions", "/v1/messages", "/messages"}:
        raise ValueError("unsupported model API path")
    return base_url + path


def make_handler(config: ProxyConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenCollabEvalProxy/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
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
            try:
                url = _upstream_url(config.upstream_base_url, self.path)
                headers = {
                    "Authorization": f"Bearer {config.upstream_api_key}",
                    "x-api-key": config.upstream_api_key,
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "Accept": self.headers.get("Accept", "application/json"),
                    "User-Agent": self.headers.get("User-Agent", "opencollab-eval"),
                }
                for name in ("anthropic-version", "anthropic-beta"):
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                request = urllib.request.Request(url, data=body, headers=headers, method="POST")
                try:
                    response = urllib.request.urlopen(request, timeout=config.timeout)
                except urllib.error.HTTPError as exc:
                    response = exc
            except (OSError, ValueError, urllib.error.URLError):
                self._json(502, {"error": "upstream_request_failed"})
                return
            with response:
                self.send_response(int(response.status))
                self.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type", "application/json"),
                )
                request_id = response.headers.get("x-request-id", "")
                if request_id:
                    self.send_header("x-request-id", request_id)
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("proxy host must be loopback")
    config = load_proxy_config(args.env_file, upstream_base_url=args.upstream_base_url, timeout=args.timeout)
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
