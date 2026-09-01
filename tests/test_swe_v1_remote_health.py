from __future__ import annotations

from opencollab_eval.engine import swe_v1_remote_records as remote_records


def test_remote_health_retries_transient_connection_reset(monkeypatch) -> None:
    attempts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            return b"ok"

    def flaky_urlopen(_url, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise ConnectionResetError("proxy restarted")
        return Response()

    monkeypatch.setattr(remote_records.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(remote_records.time, "sleep", lambda _seconds: None)

    result = remote_records.http_health("http://proxy/healthz", timeout=1)

    assert result["ok"] is True
    assert len(attempts) == 2
    assert all(value > 0 for value in attempts)


def test_remote_health_does_not_retry_deterministic_http_client_error(monkeypatch) -> None:
    attempts = []

    def forbidden_urlopen(_url, timeout):
        attempts.append(timeout)
        raise remote_records.urllib.error.HTTPError(
            "http://proxy/healthz", 401, "unauthorized", {}, None
        )

    monkeypatch.setattr(remote_records.urllib.request, "urlopen", forbidden_urlopen)
    result = remote_records.http_health("http://proxy/healthz", timeout=1)

    assert result == {"ok": False, "status": 401, "body": ""}
    assert len(attempts) == 1


def test_remote_health_retries_transient_http_server_error(monkeypatch) -> None:
    attempts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            return b"ok"

    def flaky_urlopen(_url, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise remote_records.urllib.error.HTTPError(
                "http://proxy/healthz", 503, "warming up", {}, None
            )
        return Response()

    monkeypatch.setattr(remote_records.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(remote_records.time, "sleep", lambda _seconds: None)

    result = remote_records.http_health("http://proxy/healthz", timeout=1)

    assert result["ok"] is True
    assert len(attempts) == 2
