"""Probe HTTP behavior against local endpoints with a fixed fake credential."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from opencollab_eval.experiment import model_fork_audit, model_fork_probe
from opencollab_eval.experiment.model_fork_probe import Response, http_sender, probe_context, run_probes

FAKE_KEY = "FAKE-LOCAL-PROBE-KEY"
VALID_CALL = {
    "id": "call_1", "type": "function",
    "function": {"name": "echo_probe", "arguments": '{"value":"ping"}'},
}


def _tool_body(calls: object, *, content: str | None = None) -> dict:
    return {"choices": [{"message": {"content": content, "tool_calls": calls}, "finish_reason": "tool_calls"}]}


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


@pytest.mark.parametrize("case", ["non-json", "read-timeout", "invalid-shape"])
def test_unusable_http_200_response_is_reported_without_an_input_boundary(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _clear_network_environment(monkeypatch)

    class UnusableResponse(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            if case == "read-timeout":
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"choices":')
                self.wfile.flush()
                time.sleep(0.2)
                return
            body = (
                f"gateway temporarily unavailable; Bearer {FAKE_KEY}".encode()
                if case == "non-json"
                else b'{"choices": [{"message": 42}]}'
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(UnusableResponse) as origin:
        sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY, timeout=0.05)
        report = run_probes("fake", requested="context", max_requests=1, sender=sender, ceiling_tokens=8_000)

    result = report["results"]["context"]
    attempt = result["attempts"][0]
    assert attempt["http_status"] == 200
    assert attempt["verdict"] == "inconclusive"
    assert result["smallest_input_that_failed"] is None
    assert result["inconclusive_input_sizes"] == [4_500]
    assert attempt["endpoint_said"]
    assert ("TimeoutError" if case == "read-timeout" else "Invalid") in attempt["endpoint_said"]
    assert FAKE_KEY not in json.dumps(report)


def test_long_http_200_echo_is_classified_from_the_complete_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_network_environment(monkeypatch)

    class LongEcho(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = payload["messages"][0]["content"]
            head = prompt.split("HEAD-CODE ")[1].split("\n")[0]
            tail = prompt.split("TAIL-CODE ")[1].split("\n")[0]
            answer = head + " " + ("longword " * 80) + tail
            body = json.dumps({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(LongEcho) as origin:
        sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY)
        report = run_probes("fake", requested="context", max_requests=1, sender=sender, ceiling_tokens=8_000)

    result = report["results"]["context"]
    assert result["attempts"][0]["verdict"] == "both-sentinels"
    assert result["smallest_input_that_failed"] is None
    assert result["largest_input_answered_with_both_sentinels"] == 4_500
    assert len(Response(200, body={"choices": [{"message": {"content": "x" * 700}}]}).text) == 600


def test_invalid_http_200_does_not_count_as_a_tool_or_token_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_network_environment(monkeypatch)

    class InvalidCompletion(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"choices": [{"message": {"tool_calls": 3}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(InvalidCompletion) as origin:
        sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY)
        report = run_probes("fake", requested="tool_choice,max_tokens", max_requests=5, sender=sender)

    for row in (*report["results"]["tool_choice"].values(), *report["results"]["max_tokens"].values()):
        assert row["http_status"] == 200
        assert row["inconclusive"] is True
        assert "Invalid chat completion" in row["endpoint_said"]
    assert report["results"]["tool_choice"]["auto"]["tool_calls"] is None
    assert report["results"]["max_tokens"]["max_tokens"]["completion_tokens"] is None


@pytest.mark.parametrize(
    ("calls", "valid"),
    [
        ([None], False),
        ([42], False),
        ([{}], False),
        ([{"id": "call_1", "type": "function"}], False),
        ([{"id": "call_1", "type": "function", "function": None}], False),
        ([{"id": "call_1", "type": "function", "function": {"name": 42, "arguments": "{}"}}], False),
        ([{**VALID_CALL, "id": ""}], False),
        ([{key: value for key, value in VALID_CALL.items() if key != "id"}], False),
        ([{**VALID_CALL, "type": "other"}], False),
        ([{**VALID_CALL, "function": {"name": "other", "arguments": '{"value":"ping"}'}}], False),
        ([{**VALID_CALL, "function": {"name": "echo_probe"}}], False),
        ([{**VALID_CALL, "function": {"name": "echo_probe", "arguments": "{"}}], False),
        ([{**VALID_CALL, "function": {"name": "echo_probe", "arguments": "[]"}}], False),
        ([{**VALID_CALL, "function": {"name": "echo_probe", "arguments": '{"value":null}'}}], False),
        ([VALID_CALL, None], False),
        ([], True),
        (None, True),
        ([VALID_CALL, {**VALID_CALL, "id": "call_2"}], True),
    ],
)
def test_custom_sender_counts_only_valid_tool_calls(calls: object, valid: bool) -> None:
    response = Response(200, body=_tool_body(calls, content="I cannot call that tool" if calls == [] else None))
    result = model_fork_probe.probe_tool_choice("fake", lambda _: response, budget=1)["auto"]

    assert result["http_status"] == 200
    assert result["tool_calls"] == (len(calls or []) if valid else None)
    assert result.get("inconclusive", False) is not valid


def test_http_and_custom_senders_agree_on_tool_call_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_network_environment(monkeypatch)
    cases = {
        "null-element": [None],
        "mixed": [VALID_CALL, None],
        "refusal": [],
        "multiple": [VALID_CALL, {**VALID_CALL, "id": "call_2"}],
    }

    class ToolCompletion(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            case = self.path.split("/")[1]
            content = "I cannot call that tool" if case == "refusal" else None
            body = json.dumps(_tool_body(cases[case], content=content)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(ToolCompletion) as origin:
        for case, calls in cases.items():
            sender = http_sender(f"http://127.0.0.1:{origin.server_port}/{case}", FAKE_KEY)
            http_result = model_fork_probe.probe_tool_choice("fake", sender, budget=1)["auto"]
            content = "I cannot call that tool" if case == "refusal" else None
            direct = Response(200, body=_tool_body(calls, content=content))
            direct_result = model_fork_probe.probe_tool_choice(
                "fake", lambda _, response=direct: response, budget=1
            )["auto"]
            assert http_result["http_status"] == direct_result["http_status"] == 200
            assert http_result["tool_calls"] == direct_result["tool_calls"]
            assert http_result.get("inconclusive", False) == direct_result.get("inconclusive", False)
            assert ("Invalid chat completion" in http_result["endpoint_said"]) is (case in {"null-element", "mixed"})


@pytest.mark.parametrize("case", ["non-json", "invalid-shape"])
def test_secret_crossing_the_diagnostic_excerpt_boundary_is_redacted_first(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _clear_network_environment(monkeypatch)
    body = (
        ("x" * 590 + FAKE_KEY).encode()
        if case == "non-json"
        else json.dumps({"error": "x" * 580 + FAKE_KEY}).encode()
    )

    class LongError(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(LongError) as origin:
        sender = http_sender(f"http://127.0.0.1:{origin.server_port}/v1", FAKE_KEY)
        response = sender({"model": "fake", "messages": [{"role": "user", "content": "ping"}]})

    assert response.invalid_response
    assert "[REDACTED" in response.error_text
    assert "FAKE-" not in response.error_text
    assert len(response.text) <= 600


def test_long_context_error_is_classified_from_the_complete_error() -> None:
    result = probe_context(
        "fake", lambda _: Response(400, error_text="x" * 650 + " maximum context length exceeded"),
        budget=1, ceiling_tokens=8_000,
    )
    assert result["attempts"][0]["verdict"] == "refused"
    assert len(result["attempts"][0]["endpoint_said"]) == 600


def test_http_error_read_timeout_closes_body_and_keeps_report_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutBody(io.BytesIO):
        def read(self, *_: object) -> bytes:
            raise TimeoutError(f"timed out while reading Bearer {FAKE_KEY} context_length_exceeded")

    body = TimedOutBody(b"ignored")

    def fake_open(request: object, *, timeout: float) -> None:
        raise urllib.error.HTTPError("http://127.0.0.1/", 400, "Bad Request", {}, body)

    monkeypatch.setattr(model_fork_probe.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=fake_open))
    sender = http_sender("http://127.0.0.1", FAKE_KEY)
    result = probe_context("fake", sender, budget=1, ceiling_tokens=8_000)
    assert body.closed
    assert result["attempts"][0]["verdict"] == "inconclusive"
    assert result["smallest_input_that_failed"] is None
    assert "TimeoutError" in result["attempts"][0]["endpoint_said"]
    assert FAKE_KEY not in json.dumps(result)


def test_a_failed_live_probe_preserves_the_complete_audit_and_later_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_network_environment(monkeypatch)

    class OneFailedProbe(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "HEAD-CODE" in payload["messages"][0]["content"]:
                body = b"gateway temporarily unavailable"
            else:
                body = json.dumps({
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 2},
                }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    with _serve(OneFailedProbe) as origin:
        monkeypatch.setenv("OPENCOLLAB_BASE_URL", f"http://127.0.0.1:{origin.server_port}/v1")
        monkeypatch.setenv("OPENCOLLAB_API_KEY", FAKE_KEY)
        code = model_fork_audit.run([
            "oc-audit-unlisted-model-20260927", "--json", "--probe", "context,tool_choice,max_tokens",
            "--probe-max-requests", "8",
        ])

    output = capsys.readouterr().out
    report = json.loads(output)
    assert code == 1
    assert report["failed"] is True
    assert sum(row["verdict"] == "FAIL" for row in report["findings"]) > 1
    assert report["probe"]["results"]["context"]["attempts"][0]["verdict"] == "inconclusive"
    assert report["probe"]["results"]["context"]["smallest_input_that_failed"] is None
    assert report["probe"]["results"]["tool_choice"]["auto"]["http_status"] == 200
    assert report["probe"]["results"]["max_tokens"]["max_tokens"]["completion_tokens"] == 2
    assert FAKE_KEY not in output
