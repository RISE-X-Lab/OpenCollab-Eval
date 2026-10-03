"""Exercise response cancellation and physical lease ownership without services."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import llm_api_proxy as proxy
from opencollab_eval.commands.provider_limits import install


def _handler(config, streaming=False, aggregate=False):
    handler_type = proxy.make_handler(config)
    handler = handler_type.__new__(handler_type)
    body = json.dumps({"model": "fixture", "input": "ordinary request", "stream": streaming}).encode()
    handler.path = "/v1/chat/completions" if aggregate else "/v1/responses"
    handler.headers = {"Authorization": "Bearer fixture-client", "Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.connection = object()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda: None
    return handler


def _limited_response(monkeypatch, tmp_path, response, cancelled):
    connection = SimpleNamespace(sock=None, close=lambda: None)
    transport = proxy._DirectResponse(response, connection)
    monkeypatch.setattr(proxy, "_open_direct_upstream", lambda *a, **k: transport)
    monkeypatch.setattr(proxy, "_client_disconnected", lambda client: cancelled.is_set())
    monkeypatch.setattr(proxy, "_diagnostic", lambda *a, **k: None)
    monkeypatch.delattr(proxy, "_upstream_provider_limits", raising=False)
    config_path = tmp_path / "quota.json"
    config_path.write_text(json.dumps({
        "lock_root": str(tmp_path / "slots"), "providers": {
            "fixture": {"origin": "https://fixture.invalid", "name": "Fixture", "max_active": 1},
        },
    }))
    limiter = install(proxy, config_path)
    config = proxy.ProxyConfig(
        client_token="fixture-client", upstream_api_key="test-key",
        upstream_base_url="https://fixture.invalid/v1", timeout=5, direct_upstream=True,
    )
    return limiter, config, transport


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P2-12: quiet response read ignores caller cancellation")
@pytest.mark.parametrize("streaming", [False, True])
def test_after_header_cancellation_closes_response_before_releasing_physical_lease(monkeypatch, tmp_path, streaming):
    entered, close_started, closed = threading.Event(), threading.Event(), threading.Event()
    allow_close, cancel = threading.Event(), threading.Event()
    errors = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}

        def read(self, size=-1):
            entered.set()
            assert closed.wait(3)
            return b""

        read1 = read

        def close(self):
            close_started.set()
            assert allow_close.wait(3)
            closed.set()

    limiter, config, transport = _limited_response(monkeypatch, tmp_path, Response(), cancel)
    handler = _handler(config, streaming)

    def run():
        try:
            handler.do_POST()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        assert limiter.snapshot()["service_active"] == 1
        cancel.set()
        assert close_started.wait(0.5)
        assert limiter.snapshot()["service_active"] == 1
        assert not closed.is_set()
        allow_close.set()
        worker.join(1)
        assert closed.is_set() and not worker.is_alive()
        assert limiter.snapshot()["service_active"] == 0
        assert not errors
    finally:
        allow_close.set()
        closed.set()
        worker.join(4)
        transport.close()
        monkeypatch.delattr(proxy, "_upstream_provider_limits", raising=False)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("read_error", [None, OSError, RuntimeError])
def test_direct_response_completion_and_read_failure_close_and_release(monkeypatch, tmp_path, streaming, read_error):
    closed = threading.Event()

    class Response:
        status = 200
        headers = {"Content-Type": "text/event-stream" if streaming else "application/json"}

        def read(self, size=-1):
            if read_error is not None:
                raise read_error("ordinary read failure")
            return b""

        read1 = read

        def close(self):
            closed.set()

    limiter, config, _transport = _limited_response(monkeypatch, tmp_path, Response(), threading.Event())
    try:
        if read_error is None:
            _handler(config, streaming).do_POST()
        else:
            with pytest.raises(read_error, match="ordinary read failure"):
                _handler(config, streaming).do_POST()
        assert closed.is_set()
        assert limiter.snapshot()["service_active"] == 0
    finally:
        monkeypatch.delattr(proxy, "_upstream_provider_limits", raising=False)
