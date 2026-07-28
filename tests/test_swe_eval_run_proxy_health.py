from __future__ import annotations

import hashlib
import json

from opencollab_eval.commands import swe_eval_run


def test_local_relay_health_accepts_v1_base(monkeypatch) -> None:
    captured = {}
    upstream = "https://api.example.invalid/v1"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "kind": "authenticated_model_relay",
                    "aggregate_chat_stream": True,
                    "upstream_base_url_sha256": hashlib.sha256(upstream.encode()).hexdigest(),
                }
            ).encode()

    def fake_urlopen(url, **_kwargs):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(swe_eval_run.urllib.request, "urlopen", fake_urlopen)

    assert swe_eval_run._local_relay_healthy("http://127.0.0.1:8879/v1", upstream) is True
    assert captured["url"] == "http://127.0.0.1:8879/healthz"
