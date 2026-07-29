"""Aggregate OpenAI-compatible chat-completion SSE into one JSON response."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from opencollab_eval.engine.solver_backend import (
    is_kimi_direct_model,
    kimi_response_model_matches,
)


class ChatStreamError(ValueError):
    """The upstream stream cannot prove a complete chat completion."""


def _without_schema_annotations(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_schema_annotations(item)
            for key, item in value.items()
            if key not in {"description", "title", "examples", "default", "$comment"}
        }
    if isinstance(value, list):
        return [_without_schema_annotations(item) for item in value]
    return value


def streaming_chat_request(
    body: bytes,
    *,
    compact_tool_schemas: bool = False,
) -> tuple[bytes, bool, str]:
    """Enable upstream streaming while preserving every caller parameter."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatStreamError("request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ChatStreamError("request body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ChatStreamError("request model must be a non-empty string")
    if payload.get("stream") is True:
        return body, False, model
    if compact_tool_schemas and "tools" in payload:
        payload["tools"] = _without_schema_annotations(payload["tools"])
    options = payload.get("stream_options")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ChatStreamError("stream_options must be a JSON object")
    payload["stream"] = True
    payload["stream_options"] = {**options, "include_usage": True}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(), True, model


@dataclass
class _ToolCall:
    call_id: str = ""
    call_type: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def add(self, value: dict[str, Any]) -> None:
        call_id = value.get("id")
        if isinstance(call_id, str):
            if self.call_id and call_id != self.call_id:
                raise ChatStreamError("tool call id changed during streaming")
            self.call_id = call_id
        call_type = value.get("type")
        if isinstance(call_type, str):
            if self.call_type and call_type != self.call_type:
                raise ChatStreamError("tool call type changed during streaming")
            self.call_type = call_type
        function = value.get("function") or {}
        if not isinstance(function, dict):
            raise ChatStreamError("tool call function must be an object")
        name = function.get("name")
        if isinstance(name, str):
            self.name += name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments.append(arguments)

    def finish(self) -> dict[str, Any]:
        if not self.call_id or not self.name:
            raise ChatStreamError("tool call is missing its id or function name")
        return {
            "id": self.call_id,
            "type": self.call_type or "function",
            "function": {"name": self.name, "arguments": "".join(self.arguments)},
        }


@dataclass
class _Choice:
    role: str = "assistant"
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    refusal: list[str] = field(default_factory=list)
    tool_calls: dict[int, _ToolCall] = field(default_factory=dict)
    finish_reason: str | None = None
    logprobs: Any = None

    def add(self, value: dict[str, Any]) -> None:
        delta = value.get("delta") or {}
        if not isinstance(delta, dict):
            raise ChatStreamError("choice delta must be an object")
        role = delta.get("role")
        if isinstance(role, str):
            self.role = role
        for key, target in (
            ("content", self.content),
            ("reasoning_content", self.reasoning),
            ("refusal", self.refusal),
        ):
            part = delta.get(key)
            if isinstance(part, str):
                target.append(part)
        calls = delta.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ChatStreamError("delta tool_calls must be an array")
        for call in calls:
            if not isinstance(call, dict):
                raise ChatStreamError("tool call delta must be an object")
            index = call.get("index")
            if not isinstance(index, int) or index < 0:
                raise ChatStreamError("tool call delta requires a non-negative index")
            self.tool_calls.setdefault(index, _ToolCall()).add(call)
        finish_reason = value.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise ChatStreamError("finish_reason must be a string")
            self.finish_reason = finish_reason
        if value.get("logprobs") is not None:
            self.logprobs = value["logprobs"]

    def finish(self, index: int) -> dict[str, Any]:
        if self.finish_reason is None:
            raise ChatStreamError(f"choice {index} has no terminal finish_reason")
        message: dict[str, Any] = {
            "role": self.role,
            "content": "".join(self.content) if self.content else None,
        }
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.refusal:
            message["refusal"] = "".join(self.refusal)
        if self.tool_calls:
            message["tool_calls"] = [
                self.tool_calls[call_index].finish()
                for call_index in sorted(self.tool_calls)
            ]
        return {
            "index": index,
            "message": message,
            "finish_reason": self.finish_reason,
            "logprobs": self.logprobs,
        }


def _set_read_timeout(stream: BinaryIO, timeout: float) -> None:
    candidates = (
        stream,
        getattr(stream, "fp", None),
        getattr(getattr(stream, "fp", None), "raw", None),
        getattr(getattr(getattr(stream, "fp", None), "raw", None), "_sock", None),
    )
    for candidate in reversed(candidates):
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(max(0.001, timeout))
            return


def _events(stream: BinaryIO, *, byte_limit: int, deadline: float):
    pending: list[str] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChatStreamError("upstream stream exceeded its total deadline")
        _set_read_timeout(stream, remaining)
        line = stream.readline(byte_limit + 1)
        if not line:
            if pending:
                yield "\n".join(pending)
            return
        total += len(line)
        if total > byte_limit or len(line) > byte_limit:
            raise ChatStreamError("upstream stream exceeded the response byte limit")
        try:
            text = line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise ChatStreamError("upstream stream is not valid UTF-8") from exc
        if not text:
            if pending:
                yield "\n".join(pending)
                pending.clear()
            continue
        if text.startswith(":"):
            continue
        if text.startswith("data:"):
            pending.append(text[5:].lstrip(" "))


def aggregate_chat_stream(
    stream: BinaryIO,
    *,
    byte_limit: int,
    timeout: float,
    expected_model: str,
) -> bytes:
    """Return a standard non-streaming ChatCompletion JSON document."""
    identity: dict[str, Any] = {}
    choices: dict[int, _Choice] = {}
    usage: dict[str, Any] | None = None
    done = False
    deadline = time.monotonic() + max(0.001, timeout)
    for data in _events(stream, byte_limit=byte_limit, deadline=deadline):
        if data.strip() == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ChatStreamError("upstream SSE data is not valid JSON") from exc
        if not isinstance(chunk, dict):
            raise ChatStreamError("upstream SSE data must be a JSON object")
        if chunk.get("error") is not None:
            raise ChatStreamError("upstream SSE reported an error")
        for key in (
            "id",
            "created",
            "model",
            "system_fingerprint",
            "service_tier",
        ):
            value = chunk.get(key)
            if value is None:
                continue
            if key in identity and identity[key] != value:
                raise ChatStreamError(f"stream identity changed for {key}")
            identity[key] = value
        if chunk.get("usage") is not None:
            if not isinstance(chunk["usage"], dict):
                raise ChatStreamError("stream usage must be an object")
            usage = chunk["usage"]
        chunk_choices = chunk.get("choices") or []
        if not isinstance(chunk_choices, list):
            raise ChatStreamError("stream choices must be an array")
        for position, value in enumerate(chunk_choices):
            if not isinstance(value, dict):
                raise ChatStreamError("stream choice must be an object")
            index = value.get("index", position)
            if not isinstance(index, int) or index < 0:
                raise ChatStreamError("choice index must be a non-negative integer")
            choices.setdefault(index, _Choice()).add(value)
    if not done:
        raise ChatStreamError("upstream stream ended without [DONE]")
    if not choices:
        raise ChatStreamError("upstream stream ended without a choice")
    actual_model = identity.get("model")
    model_matches = (
        kimi_response_model_matches(expected_model, actual_model)
        if is_kimi_direct_model(expected_model)
        else actual_model == expected_model
    )
    if not model_matches:
        raise ChatStreamError("stream response model does not match the request")
    result = {
        **identity,
        "object": "chat.completion",
        "choices": [choices[index].finish(index) for index in sorted(choices)],
    }
    if usage is not None:
        result["usage"] = usage
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > byte_limit:
        raise ChatStreamError("aggregated response exceeded the response byte limit")
    return encoded


__all__ = [
    "ChatStreamError",
    "aggregate_chat_stream",
    "streaming_chat_request",
]
