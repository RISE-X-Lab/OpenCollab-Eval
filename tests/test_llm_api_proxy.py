from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openai.types.chat import ChatCompletion

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
        self.send_header("Retry-After", "7")
        self.end_headers()
        self.wfile.write(payload)


class _AggregatingUpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    status = 200
    content_type = "text/event-stream"
    response = b""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.send_header("x-request-id", "stream-request-1")
        self.end_headers()
        self.wfile.write(self.response)


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
    assert health["compact_tool_schemas"] is False
    assert health["responses_passthrough"] is True
    assert health["allow_insecure_upstream"] is False
    assert health["upstream_timeout"] == 5
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
        assert response.headers["Retry-After"] == "7"
        assert json.load(response)["choices"][0]["message"]["content"] == "OK"
    observed = _UpstreamHandler.requests[0]
    observed_headers = {key.lower(): value for key, value in observed["headers"].items()}
    assert observed["path"] == "/coding/v1/chat/completions"
    assert observed_headers["authorization"] == "Bearer upstream-secret"
    assert observed_headers["x-api-key"] == "upstream-secret"
    assert observed_headers["user-agent"] == "OpenAI/Python test"
    assert observed_headers["anthropic-version"] == "2023-06-01"
    assert observed_headers["anthropic-beta"] == "context-1m-2025-08-07"

    responses_request = urllib.request.Request(
        relay + "/v1/responses",
        data=b'{"model":"gpt-5.6-sol","input":"OK","store":false}',
        headers={
            "Authorization": "Bearer client-secret",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(responses_request, timeout=2) as response:
        assert response.status == 200
    assert _UpstreamHandler.requests[1]["path"] == "/coding/v1/responses"


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


def test_proxy_enforces_declared_upstream_request_limit_after_processing() -> None:
    _UpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                max_upstream_request_bytes=16,
            )
        ),
    )
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    upstream_thread.start()
    proxy_thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/responses",
        data=b'{"model":"gpt-5.6-sol","input":"too large"}',
        headers={"Authorization": "Bearer client-secret"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        assert caught.value.code == 413
        assert json.load(caught.value)["error"] == "upstream_request_too_large"
        assert _UpstreamHandler.requests == []
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


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


def test_proxy_config_requires_explicit_opt_in_for_http_upstream(tmp_path) -> None:
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "OPENCOLLAB_PROXY_CLIENT_TOKEN=client\n"
        "OPENCOLLAB_UPSTREAM_API_KEY=upstream\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    with pytest.raises(ValueError, match="HTTPS origin"):
        load_proxy_config(env_file, upstream_base_url="http://api.example.invalid/v1")
    config = load_proxy_config(
        env_file,
        upstream_base_url="http://api.example.invalid/v1",
        allow_insecure_upstream=True,
    )

    assert config.allow_insecure_upstream is True


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_proxy_config_rejects_invalid_upstream_timeout(tmp_path, timeout) -> None:
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "OPENCOLLAB_PROXY_CLIENT_TOKEN=client\nOPENCOLLAB_UPSTREAM_API_KEY=upstream\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    with pytest.raises(ValueError, match="positive finite"):
        load_proxy_config(
            env_file,
            upstream_base_url="https://api.example.invalid/v1",
            timeout=timeout,
        )


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


def test_proxy_aggregates_chat_stream_without_losing_request_or_response_evidence() -> None:
    _AggregatingUpstreamHandler.requests = []
    _AggregatingUpstreamHandler.status = 200
    _AggregatingUpstreamHandler.content_type = "text/event-stream"
    _AggregatingUpstreamHandler.response = (
        b'data: {"id":"chat-3","created":789,"model":"gpt-5.6-sol","choices":'
        b'[{"index":0,"delta":{"role":"assistant","content":"O"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chat-3","created":789,"model":"gpt-5.6-sol","choices":'
        b'[{"index":0,"delta":{"content":"K"},"finish_reason":"stop"}]}\n\n'
        b'data: {"id":"chat-3","created":789,"model":"gpt-5.6-sol","choices":[],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
        b"data: [DONE]\n\n"
    )
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _AggregatingUpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                aggregate_chat_stream=True,
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
    request_body = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "xhigh",
        "temperature": 0.2,
        "stream_options": {"opaque": "kept"},
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=json.dumps(request_body).encode(),
        headers={"Authorization": "Bearer client-secret", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
            assert response.headers["x-request-id"] == "stream-request-1"
        forwarded = json.loads(_AggregatingUpstreamHandler.requests[0]["body"])
        assert forwarded == {
            **request_body,
            "stream": True,
            "stream_options": {"opaque": "kept", "include_usage": True},
        }
        assert result["model"] == "gpt-5.6-sol"
        assert result["choices"][0]["message"]["content"] == "OK"
        assert result["usage"]["total_tokens"] == 12
        ChatCompletion.model_validate(result)
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


def test_proxy_preserves_client_stream_and_upstream_error_status() -> None:
    _AggregatingUpstreamHandler.requests = []
    _AggregatingUpstreamHandler.status = 429
    _AggregatingUpstreamHandler.content_type = "application/json"
    _AggregatingUpstreamHandler.response = b'{"error":{"message":"limited"}}'
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _AggregatingUpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                aggregate_chat_stream=True,
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
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=b'{"model":"gpt-5.6-sol","stream":true}',
        headers={"Authorization": "Bearer client-secret"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        assert caught.value.code == 429
        assert json.loads(_AggregatingUpstreamHandler.requests[0]["body"])["stream"] is True
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


def test_proxy_preserves_successful_client_chat_stream_byte_for_byte() -> None:
    _AggregatingUpstreamHandler.requests = []
    _AggregatingUpstreamHandler.status = 200
    _AggregatingUpstreamHandler.content_type = "text/event-stream"
    _AggregatingUpstreamHandler.response = (
        b'data: {"id":"chat-7","model":"gpt-5.6-sol","choices":[]}\n\n'
        b"data: [DONE]\n\n"
    )
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _AggregatingUpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                aggregate_chat_stream=True,
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
    body = b'{"model":"gpt-5.6-sol","messages":[],"stream":true}'
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer client-secret"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            assert response.read() == _AggregatingUpstreamHandler.response
        assert _AggregatingUpstreamHandler.requests[0]["body"] == body
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
def test_proxy_rejects_non_sse_success_during_aggregation(content_type: str) -> None:
    _AggregatingUpstreamHandler.requests = []
    _AggregatingUpstreamHandler.status = 200
    _AggregatingUpstreamHandler.content_type = content_type
    _AggregatingUpstreamHandler.response = b'{"choices":[]}'
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _AggregatingUpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                aggregate_chat_stream=True,
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
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=b'{"model":"gpt-5.6-sol","messages":[]}',
        headers={"Authorization": "Bearer client-secret"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        assert caught.value.code == 502
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


def test_proxy_aborts_stream_that_stalls_after_first_chunk() -> None:
    class StallingHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"id":"chat-8","model":"gpt-5.6-sol","choices":'
                b'[{"index":0,"delta":{"content":"A"},"finish_reason":null}]}\n\n'
            )
            self.wfile.flush()
            time.sleep(0.25)
            self.wfile.write(
                b'data: {"id":"chat-8","model":"gpt-5.6-sol","choices":'
                b'[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StallingHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=0.05,
                aggregate_chat_stream=True,
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
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=b'{"model":"gpt-5.6-sol","messages":[]}',
        headers={"Authorization": "Bearer client-secret"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        assert caught.value.code == 502
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
