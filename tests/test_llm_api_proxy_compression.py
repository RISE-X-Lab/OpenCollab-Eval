from __future__ import annotations

import gzip
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from llm_api_proxy_compression_support import RecordingUpstreamHandler

from opencollab_eval.commands.llm_api_proxy import ProxyConfig, make_handler


def _start_relay(**config_overrides: object) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    RecordingUpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            ProxyConfig(
                client_token="client-secret",
                upstream_api_key="upstream-secret",
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                timeout=5,
                direct_upstream=True,
                **config_overrides,
            )
        ),
    )
    proxy.daemon_threads = True
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return upstream, proxy


def _post_json(proxy: ThreadingHTTPServer, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": "Bearer client-secret", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        assert response.status == 200


def _stop_relay(*servers: ThreadingHTTPServer) -> None:
    for server in servers:
        server.shutdown()
        server.server_close()


def test_proxy_gzips_upstream_request_without_changing_json() -> None:
    upstream, proxy = _start_relay(gzip_upstream_request=True)
    payload = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "x" * 5000}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        _post_json(proxy, payload)
        observed = RecordingUpstreamHandler.requests[0]
        headers = {key.lower(): value for key, value in observed["headers"].items()}
        assert headers["content-encoding"] == "gzip"
        assert len(observed["body"]) < len(body)
        assert gzip.decompress(observed["body"]) == body
    finally:
        _stop_relay(proxy, upstream)


def test_proxy_compacts_tool_schemas_without_enabling_streaming() -> None:
    upstream, proxy = _start_relay(compact_tool_schemas=True)
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "inspect"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "Long prose.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repository path."}},
                    "required": ["path"],
                },
            },
        }],
    }
    try:
        _post_json(proxy, payload)
        observed = json.loads(RecordingUpstreamHandler.requests[0]["body"])
        assert "stream" not in observed
        assert "description" not in observed["tools"][0]["function"]
        path_schema = observed["tools"][0]["function"]["parameters"]["properties"]["path"]
        assert "description" not in path_schema
    finally:
        _stop_relay(proxy, upstream)
