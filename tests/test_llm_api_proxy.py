from __future__ import annotations

import hashlib
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opencollab_eval.commands.llm_api_proxy import (
    ProxyConfig,
    load_proxy_config,
    make_handler,
    upstream_base_url_sha256,
)
from opencollab_eval.commands.swe_v1_prolite_common import _redacted


class _UpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("x-request-id", "request-1")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def relay():
    _UpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    config = ProxyConfig(
        client_token="client-secret",
        upstream_api_key="upstream-secret",
        upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/coding/v1",
        timeout=5,
    )
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
    proxy.daemon_threads = True
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        yield f"http://127.0.0.1:{proxy.server_port}"
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


def test_proxy_health_and_authenticated_forwarding(relay: str) -> None:
    with urllib.request.urlopen(relay + "/healthz", timeout=2) as response:
        health = json.load(response)
    assert health["status"] == "ok"
    assert len(health["upstream_base_url_sha256"]) == 64
    request = urllib.request.Request(
        relay + "/chat/completions",
        data=b'{"model":"kimi-for-coding","messages":[]}',
        headers={
            "Authorization": "Bearer client-secret",
            "Content-Type": "application/json",
            "User-Agent": "OpenAI/Python test",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "context-1m-2025-08-07",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        assert response.headers["x-request-id"] == "request-1"
        assert json.load(response)["choices"][0]["message"]["content"] == "OK"
    observed = _UpstreamHandler.requests[0]
    observed_headers = {key.lower(): value for key, value in observed["headers"].items()}
    assert observed["path"] == "/coding/v1/chat/completions"
    assert observed_headers["authorization"] == "Bearer upstream-secret"
    assert observed_headers["x-api-key"] == "upstream-secret"
    assert observed_headers["user-agent"] == "OpenAI/Python test"
    assert observed_headers["anthropic-version"] == "2023-06-01"
    assert observed_headers["anthropic-beta"] == "context-1m-2025-08-07"


def test_proxy_rejects_wrong_token_without_contacting_upstream(relay: str) -> None:
    request = urllib.request.Request(
        relay + "/chat/completions",
        data=b"{}",
        headers={"Authorization": "Bearer wrong"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=2)
    assert caught.value.code == 401
    assert _UpstreamHandler.requests == []


def test_proxy_config_requires_private_file_and_separate_tokens(tmp_path) -> None:
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "OPENCOLLAB_PROXY_CLIENT_TOKEN=client\n"
        "OPENCOLLAB_UPSTREAM_API_KEY=upstream\n"
        "OPENCOLLAB_UPSTREAM_BASE_URL=https://api.kimi.example/coding/v1\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    config = load_proxy_config(env_file)
    assert config.client_token == "client"
    assert config.upstream_api_key == "upstream"
    env_file.chmod(0o644)
    with pytest.raises(PermissionError):
        load_proxy_config(env_file)


def test_proxy_health_fingerprint_binds_upstream_url() -> None:
    config = ProxyConfig("client", "upstream", "https://api.kimi.com/coding/v1", 5)

    assert upstream_base_url_sha256(config) == hashlib.sha256(
        config.upstream_base_url.encode()
    ).hexdigest()


def test_proxy_accepts_anthropic_upstream_token(tmp_path) -> None:
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "OPENCOLLAB_PROXY_CLIENT_TOKEN=client\n"
        "ANTHROPIC_AUTH_" "TOKEN=anthropic-upstream\n"
        "OPENCOLLAB_UPSTREAM_BASE_URL=https://api.anthropic.example\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    config = load_proxy_config(env_file)

    assert config.client_token == "client"
    assert config.upstream_api_key == "anthropic-upstream"


def test_proxy_secrets_are_redacted_from_diagnostics() -> None:
    value = _redacted(
        "OPENCOLLAB_PROXY_CLIENT_TOKEN=client-secret "
        "OPENCOLLAB_UPSTREAM_API_KEY=upstream-secret KIMI_API_KEY=kimi-secret"
    )
    assert "client-secret" not in value
    assert "upstream-secret" not in value
    assert "kimi-secret" not in value


def test_proxy_forwards_first_sse_event_before_upstream_closes() -> None:
    first_event_sent = threading.Event()
    release_upstream = threading.Event()

    class StreamingHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: first\ndata: one\n\n")
            self.wfile.flush()
            first_event_sent.set()
            release_upstream.wait(2)
            self.wfile.write(b"event: second\ndata: two\n\n")
            self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}",
                timeout=5,
            )
        ),
    )
    proxy.daemon_threads = True
    threads = [
        threading.Thread(target=upstream.serve_forever, daemon=True),
        threading.Thread(target=proxy.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=3)
    try:
        connection.request(
            "POST",
            "/v1/messages",
            body=b"{}",
            headers={"Authorization": "Bearer client-secret"},
        )
        response = connection.getresponse()
        received: list[bytes] = []
        first_line_read = threading.Event()

        def read_first_line() -> None:
            received.append(response.readline())
            first_line_read.set()

        reader = threading.Thread(target=read_first_line, daemon=True)
        reader.start()
        assert first_event_sent.wait(1)
        assert first_line_read.wait(0.5), "the proxy buffered the first SSE event until EOF"
        assert received == [b"event: first\n"]
        release_upstream.set()
        reader.join(1)
        assert b"event: second" in response.read()
    finally:
        release_upstream.set()
        connection.close()
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
