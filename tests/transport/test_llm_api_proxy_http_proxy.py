from __future__ import annotations

import urllib.parse
import urllib.request

import pytest

from opencollab_eval.commands.llm_api_proxy import _configured_http_proxy


def test_direct_transport_selects_an_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"http": "http://proxy.internal:8888"},
    )

    proxy = _configured_http_proxy(
        urllib.parse.urlsplit("http://gateway.example.invalid:56477/v1/responses")
    )

    assert proxy is not None
    assert proxy.hostname == "proxy.internal"
    assert proxy.port == 8888


def test_direct_transport_bypasses_proxy_for_excluded_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: True)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: pytest.fail("bypassed hosts must not inspect proxy settings"),
    )

    assert (
        _configured_http_proxy(urllib.parse.urlsplit("http://127.0.0.1:8080"))
        is None
    )


def test_direct_transport_rejects_credentialed_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://user:secret@proxy.invalid:8888"},
    )

    with pytest.raises(ValueError, match="uncredentialed HTTP proxy"):
        _configured_http_proxy(urllib.parse.urlsplit("https://api.example.invalid/v1"))
