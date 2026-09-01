from __future__ import annotations

import email.message
import importlib.util
import io
import socket
from pathlib import Path
from types import ModuleType

import pytest

RELAY = Path(__file__).parents[1] / "src/opencollab_eval/resources/claude_api_relay.py"
MAX_TIMEOUT = 6 * 60 * 60 + 60


def _load_relay(
    monkeypatch: pytest.MonkeyPatch, *, unix: bool, timeout: str
) -> ModuleType:
    if not RELAY.is_file():
        pytest.skip("relay source is unavailable in the installed-wheel contract")
    if unix:
        monkeypatch.setenv("CLAUDE_RELAY_UPSTREAM_UNIX", "/tmp/fake-upstream.sock")
        monkeypatch.delenv("CLAUDE_RELAY_UPSTREAM", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_RELAY_UPSTREAM", "http://upstream.invalid")
        monkeypatch.delenv("CLAUDE_RELAY_UPSTREAM_UNIX", raising=False)
    monkeypatch.setenv("CLAUDE_RELAY_UPSTREAM_TIMEOUT", timeout)
    name = f"claude_api_relay_test_{id(timeout)}_{unix}"
    spec = importlib.util.spec_from_file_location(name, RELAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handler(module: ModuleType):
    handler = object.__new__(module.Relay)
    handler.path = "/v1/messages"
    handler.command = "POST"
    handler.headers = email.message.Message()
    handler.headers["Content-Length"] = "0"
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None
    handler.close_connection = False
    return handler


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def read1(self, _size: int) -> bytes:
        return b""

    def read(self, _size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None


def test_relay_uses_bounded_configured_timeout_without_waiting_300_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_relay(monkeypatch, unix=False, timeout="1")
    captured: dict[str, float] = {}

    def fake_urlopen(_request, *, timeout: float):
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    _handler(module)._relay()

    assert 0 < captured["timeout"] <= 1
    assert "timeout=300" not in RELAY.read_text(encoding="utf-8")


def test_relay_applies_same_timeout_to_unix_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_relay(monkeypatch, unix=True, timeout="1")
    captured: dict[str, float] = {}

    class FakeConnection:
        def __init__(self, _path: str, *, timeout: float):
            captured["timeout"] = timeout

        def request(self, *_args, **_kwargs) -> None:
            return None

        def getresponse(self):
            return _Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "UnixHTTPConnection", FakeConnection)
    _handler(module)._relay()

    assert 0 < captured["timeout"] <= 1


@pytest.mark.parametrize("timeout", ["0", "nan", "inf", str(MAX_TIMEOUT + 0.1)])
def test_relay_rejects_non_finite_or_oversized_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    with pytest.raises(RuntimeError, match="finite, positive, and bounded"):
        _load_relay(monkeypatch, unix=True, timeout=timeout)


class _SocketProbe:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _SlowBody:
    def __init__(self, probe: _SocketProbe) -> None:
        self.probe = probe

    def read(self, _size: int) -> bytes:
        # A real socket raises this once its timeout expires.  The fake uses
        # the same signal so the test remains deterministic and fast.
        assert self.probe.timeouts and self.probe.timeouts[-1] > 0
        raise TimeoutError("client body stalled")


def test_relay_bounds_a_silent_client_body_before_contacting_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_relay(monkeypatch, unix=False, timeout="1")
    handler = _handler(module)
    probe = _SocketProbe()
    handler.connection = probe
    handler.headers.replace_header("Content-Length", "4")
    handler.rfile = _SlowBody(probe)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("upstream contacted before body completed"),
    )

    with pytest.raises(socket.timeout):
        handler._relay()
    assert probe.timeouts
    assert 0 < probe.timeouts[0] <= module.UPSTREAM_TIMEOUT


class _SlowResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self) -> None:
        self.socket = _SocketProbe()
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.socket.settimeout(value)

    def read1(self, _size: int) -> bytes:
        raise TimeoutError("upstream body stalled")

    def read(self, _size: int = -1) -> bytes:
        return self.read1(_size)

    def close(self) -> None:
        self.closed = True


def test_relay_bounds_a_stalled_upstream_stream_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_relay(monkeypatch, unix=False, timeout="1")
    handler = _handler(module)
    response = _SlowResponse()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(socket.timeout):
        handler._relay()
    assert response.closed is True
    assert response.socket.timeouts
    assert 0 < response.socket.timeouts[-1] <= module.UPSTREAM_TIMEOUT


class _SlowWriter:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def write(self, _chunk: bytes) -> int:
        raise TimeoutError("client response socket stalled")

    def flush(self) -> None:
        return None


class _OneChunkResponse(_SlowResponse):
    def read1(self, _size: int) -> bytes:
        if not hasattr(self, "sent"):
            self.sent = True
            return b"chunk"
        return b""


def test_relay_bounds_a_slow_client_response_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_relay(monkeypatch, unix=False, timeout="1")
    handler = _handler(module)
    response = _OneChunkResponse()
    writer = _SlowWriter()
    handler.wfile = writer
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(socket.timeout):
        handler._relay()
    assert response.closed is True
    assert writer.timeouts
    assert 0 < writer.timeouts[-1] <= module.UPSTREAM_TIMEOUT
