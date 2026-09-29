"""Probe HTTP behavior against local endpoints with a fixed fake credential."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opencollab_eval.experiment.model_fork_probe import Response, http_sender, probe_context, run_probes

FAKE_KEY = "FAKE-LOCAL-PROBE-KEY"


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _clear_network_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy") or name in {
            "OPENCOLLAB_BASE_URL", "OPENCOLLAB_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "OPENCOLLAB_ENV_FILE"
        }:
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_probe_rejects_every_redirect_before_forwarding_the_credential(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _clear_network_environment(monkeypatch)
    destination_requests: list[tuple[str, str | None]] = []
    origin_requests: list[str | None] = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            destination_requests.append((self.command, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_: object) -> None:
            pass

    with _serve(Destination) as destination:
        class Origin(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                origin_requests.append(self.headers.get("Authorization"))
                self.rfile.read(int(self.headers["Content-Length"]))
                body = f"Bearer {FAKE_KEY} must stay at origin".encode()
                self.send_response(status)
                self.send_header("Location", f"http://localhost:{destination.server_port}/other")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                pass

        with _serve(Origin) as origin:
            sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY)
            response = sender({"model": "fake", "messages": [{"role": "user", "content": "ping"}]})

    assert origin_requests == [f"Bearer {FAKE_KEY}"]
    assert destination_requests == []
    assert response.status == status
    assert "redirect not followed" in response.text
    assert FAKE_KEY not in response.text


def test_probe_keeps_a_direct_successful_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_network_environment(monkeypatch)
    received: list[str | None] = []

    class Success(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({
                "choices": [{"message": {"content": FAKE_KEY}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(Success) as origin:
        sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY)
        response = sender(
            {"model": "fake", "messages": [{"role": "user", "content": "ping"}]}
        )
        report = run_probes("fake", requested="max_tokens", max_requests=2, sender=sender)

    assert received == [f"Bearer {FAKE_KEY}"] * 3
    assert response.status == 200
    assert response.text == FAKE_KEY
    assert response.completion_tokens == 2
    assert FAKE_KEY not in json.dumps(report)


@pytest.mark.parametrize(
    ("status", "error_text"),
    [
        (0, "URLError timeout"),
        (302, "HTTP 302 redirect not followed"),
        (400, "Bad Request"),
        (401, "Invalid API key"),
        (413, "Request entity too large"),
        (429, "Rate limit exceeded"),
        (503, "Service temporarily unavailable"),
        (503, "context_length_exceeded in upstream error"),
    ],
)
def test_unrelated_endpoint_errors_do_not_set_an_input_boundary(status: int, error_text: str) -> None:
    result = probe_context("fake", lambda _: Response(status, error_text=error_text), budget=2, ceiling_tokens=8_000)
    assert result["requests_spent"] == 1
    assert result["attempts"][0]["verdict"] == "inconclusive"
    assert result["smallest_input_that_failed"] is None
    assert result["largest_input_answered_with_both_sentinels"] is None
    assert result["inconclusive_input_sizes"] == [4_500]
    assert result["inconclusive_reason"]


@pytest.mark.parametrize(
    ("status", "error_text"),
    [
        (400, "context_length_exceeded"),
        (400, "This model's maximum context length is 8192 tokens"),
        (400, "Range of input length should be [1, 40000]"),
        (413, "Prompt is too long for the context window"),
    ],
)
def test_explicit_context_limit_errors_keep_the_failed_boundary(status: int, error_text: str) -> None:
    result = probe_context("fake", lambda _: Response(status, error_text=error_text), budget=1, ceiling_tokens=8_000)
    assert result["attempts"][0]["verdict"] == "refused"
    assert result["smallest_input_that_failed"] == 4_500
    assert result["inconclusive_input_sizes"] == []


def test_success_without_either_sentinel_is_inconclusive() -> None:
    response = Response(200, body={"choices": [{"message": {"content": "I cannot answer"}, "finish_reason": "stop"}]})
    result = probe_context("fake", lambda _: response, budget=2, ceiling_tokens=8_000)
    assert result["attempts"][0]["verdict"] == "inconclusive"
    assert result["smallest_input_that_failed"] is None
