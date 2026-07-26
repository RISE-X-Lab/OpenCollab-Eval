"""Strict deterministic OpenAI-compatible service for the SWE end-to-end test."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FAKE_API_KEY = "opencollab-" + "e2e-fake-key"
MODEL = "kimi-for-coding"
EXPECTED_THINKING = {"type": "enabled", "keep": "all"}
SOURCE_PATH = "package b/source files/calculator.py"
PROVIDER_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DASHSCOPE_API_KEY",
    "GLM_PROXY_CLIENT_TOKEN",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENCOLLAB_API_KEY",
    "OPENCOLLAB_PROXY_CLIENT_TOKEN",
    "OPENCOLLAB_READ_TOKEN",
    "OPENAI_API_KEY",
)


def validate_generation_request(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["request body must be a JSON object"]
    expected = {
        "model": MODEL,
        "temperature": 1,
        "top_p": 0.95,
        "max_tokens": 32768,
        "thinking": EXPECTED_THINKING,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            errors.append(f"{key} must equal {value!r}")
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        errors.append("messages must be a non-empty array")
    tool_names = {
        item.get("function", {}).get("name")
        for item in payload.get("tools", [])
        if isinstance(item, dict)
    }
    required_tools = {"file_read", "file_write", "bash"}
    if not required_tools.issubset(tool_names):
        errors.append("file_read, file_write, and bash tools are required")
    return errors


def _tool_history(messages: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            name = call.get("function", {}).get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _tool_response(sequence: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-e2e-{sequence:02d}",
        "object": "chat.completion",
        "created": 1_700_000_000 + sequence,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "Deterministic test reasoning.",
                    "tool_calls": [
                        {
                            "id": f"call-e2e-{sequence:02d}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, separators=(",", ":")),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def deterministic_response(payload: dict[str, Any]) -> dict[str, Any]:
    history = _tool_history(payload["messages"])
    if history == []:
        return _tool_response(1, "file_read", {"path": SOURCE_PATH})
    if history == ["file_read"]:
        return _tool_response(
            2,
            "file_write",
            {
                "path": SOURCE_PATH,
                "mode": "str_replace",
                "old_str": "return left - right",
                "new_str": "return left + right",
            },
        )
    if history == ["file_read", "file_write"]:
        return _tool_response(
            3,
            "bash",
            {
                "command": (
                    "test ! -e leaked-answer.txt && test ! -e .cache/result && "
                    "test ! -e build/result && test ! -e nested-residue && "
                    "test -L optional-runtime && "
                    "test \"$(git rev-list --all --count)\" = 1 && "
                    "! git rev-parse --verify refs/heads/future-answer >/dev/null 2>&1 && "
                    "PYTHONDONTWRITEBYTECODE=1 python -c \"from pathlib import Path; "
                    f"ns={{}}; exec(Path('{SOURCE_PATH}').read_text(), ns); "
                    "assert ns['add'](2, 3) == 5\""
                ),
                "timeout": 30,
            },
        )
    if history == ["file_read", "file_write", "bash"]:
        return {
            "id": "chatcmpl-e2e-04",
            "object": "chat.completion",
            "created": 1_700_000_004,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Fixed calculator.add and verified add(2, 3) == 5.",
                        "reasoning_content": "Deterministic test reasoning.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
    raise ValueError(f"unexpected tool history: {history!r}")


class TraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)


def handler_class(trace: TraceWriter, ready_file: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenCollabFakeOpenAI/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {FAKE_API_KEY}"

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _reject(self, status: int, reason: str) -> None:
            trace.append({"event": "rejected", "path": self.path, "reason": reason, "time_ns": time.time_ns()})
            self._write_json(status, {"error": {"type": "invalid_request_error", "message": reason}})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/healthz":
                self._write_json(200, {"status": "ok"})
                return
            if not self._authorized():
                self._reject(401, "invalid fake credential")
                return
            if self.path == "/v1/models":
                trace.append({"event": "model_probe", "model": MODEL, "time_ns": time.time_ns()})
                self._write_json(
                    200,
                    {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "local-e2e"}]},
                )
                return
            self._reject(404, "unknown endpoint")

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._reject(401, "invalid fake credential")
                return
            if self.path != "/v1/chat/completions":
                self._reject(404, "unknown endpoint")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4 * 1024 * 1024:
                    raise ValueError("request body size is invalid")
                raw = self.rfile.read(length)
                payload = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                self._reject(400, str(exc))
                return
            errors = validate_generation_request(payload)
            if errors:
                self._reject(400, "; ".join(errors))
                return
            try:
                response = deterministic_response(payload)
            except ValueError as exc:
                self._reject(409, str(exc))
                return
            trace.append(
                {
                    "event": "generation",
                    "request_sha256": hashlib.sha256(raw).hexdigest(),
                    "message_roles": [item.get("role") for item in payload["messages"]],
                    "model": payload["model"],
                    "temperature": payload["temperature"],
                    "top_p": payload["top_p"],
                    "max_tokens": payload["max_tokens"],
                    "thinking": payload["thinking"],
                    "response_id": response["id"],
                    "time_ns": time.time_ns(),
                }
            )
            self._write_json(200, response)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--forbidden-env-value", default="")
    args = parser.parse_args()
    leaked = [
        name
        for name in PROVIDER_KEY_NAMES
        if args.forbidden_env_value
        and os.environ.get(name) == args.forbidden_env_value
    ]
    if leaked:
        raise RuntimeError("provider credential canary reached fake model environment: " + ", ".join(leaked))
    trace = TraceWriter(args.trace)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_class(trace, args.ready_file))
    args.ready_file.write_text(str(args.port) + "\n", encoding="utf-8")
    trace.append(
        {
            "event": "started",
            "port": args.port,
            "provider_environment_clean": not any(os.environ.get(name) for name in PROVIDER_KEY_NAMES),
            "time_ns": time.time_ns(),
        }
    )
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        trace.append({"event": "stopped", "time_ns": time.time_ns()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
