from __future__ import annotations

import io
import json

import pytest
from openai.types.chat import ChatCompletion

from opencollab_eval.commands import llm_api_stream
from opencollab_eval.commands.llm_api_stream import (
    ChatStreamError,
    aggregate_chat_stream,
    streaming_chat_request,
)


def _sse(*events: object, done: bool = True) -> io.BytesIO:
    lines = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event, separators=(",", ":"))
        lines.append(f"data: {payload}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return io.BytesIO("".join(lines).encode())


def _aggregate(*events: object, done: bool = True, byte_limit: int = 100_000) -> dict:
    return json.loads(
        aggregate_chat_stream(
            _sse(*events, done=done),
            byte_limit=byte_limit,
            timeout=5,
            expected_model="gpt-5.6-sol",
        )
    )


def test_streaming_request_preserves_parameters_and_merges_options() -> None:
    request = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "read"}}],
        "reasoning_effort": "xhigh",
        "temperature": 0.3,
        "top_p": 0.8,
        "max_completion_tokens": 321,
        "stream_options": {"opaque": "kept", "include_usage": False},
    }

    encoded, aggregate, model = streaming_chat_request(json.dumps(request).encode())
    actual = json.loads(encoded)

    assert aggregate is True
    assert model == "gpt-5.6-sol"
    assert actual == {
        **request,
        "stream": True,
        "stream_options": {"opaque": "kept", "include_usage": True},
    }


def test_streaming_request_leaves_caller_stream_unchanged() -> None:
    body = b'{"model":"gpt-5.6-sol","stream":true,"stream_options":{"include_usage":false}}'

    assert streaming_chat_request(body) == (body, False, "gpt-5.6-sol")


def test_streaming_request_can_drop_only_tool_schema_annotations() -> None:
    request = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "inspect"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "Long model-facing prose.",
                    "parameters": {
                        "type": "object",
                        "title": "Read arguments",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "A repository path.",
                                "minLength": 1,
                            }
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
    }

    encoded, aggregate, model = streaming_chat_request(
        json.dumps(request).encode(),
        compact_tool_schemas=True,
    )
    actual = json.loads(encoded)

    assert aggregate is True
    assert model == "gpt-5.6-sol"
    assert actual["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1}
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert actual["messages"] == request["messages"]


def test_tool_schema_compaction_does_not_require_streaming() -> None:
    request = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "inspect"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "Long model-facing prose.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "A repository path.",
                            }
                        },
                        "required": ["path"],
                    },
                },
            }
        ],
    }

    encoded, aggregate, model = streaming_chat_request(
        json.dumps(request).encode(),
        compact_tool_schemas=True,
        enable_stream=False,
    )
    actual = json.loads(encoded)

    assert aggregate is False
    assert model == "deepseek-v4-pro"
    assert "stream" not in actual
    assert actual["tools"][0]["function"]["name"] == "file_read"
    assert "description" not in actual["tools"][0]["function"]
    path_schema = actual["tools"][0]["function"]["parameters"]["properties"]["path"]
    assert "description" not in path_schema


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b'{"stream_options":1}', b'{"messages":[]}'],
)
def test_streaming_request_rejects_invalid_payload(body: bytes) -> None:
    with pytest.raises(ChatStreamError):
        streaming_chat_request(body)


def test_aggregate_preserves_identity_reasoning_parallel_tools_and_usage() -> None:
    result = _aggregate(
        {
            "id": "chat-1",
            "object": "chat.completion.chunk",
            "created": 123,
            "model": "gpt-5.6-sol",
            "system_fingerprint": "fp-1",
            "service_tier": "default",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "inspect ",
                        "content": "calling ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":'},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chat-1",
            "created": 123,
            "model": "gpt-5.6-sol",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "reasoning_content": "then act",
                        "content": "tools",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": '"a.py"}'},
                            },
                            {
                                "index": 1,
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "grep", "arguments": '{"q":"x"}'},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "chat-1",
            "created": 123,
            "model": "gpt-5.6-sol",
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        },
    )

    assert result["id"] == "chat-1"
    assert result["object"] == "chat.completion"
    assert result["model"] == "gpt-5.6-sol"
    assert result["system_fingerprint"] == "fp-1"
    assert result["service_tier"] == "default"
    message = result["choices"][0]["message"]
    assert message["content"] == "calling tools"
    assert message["reasoning_content"] == "inspect then act"
    assert message["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        },
        {
            "id": "call-2",
            "type": "function",
            "function": {"name": "grep", "arguments": '{"q":"x"}'},
        },
    ]
    assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 80
    ChatCompletion.model_validate(result)


def test_aggregate_supports_multiple_choices_and_omits_absent_usage() -> None:
    result = _aggregate(
        {
            "id": "chat-2",
            "created": 456,
            "model": "gpt-5.6-sol",
            "choices": [
                {"index": 1, "delta": {"content": "B"}, "finish_reason": "stop"},
                {"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"},
            ],
        }
    )

    assert [choice["index"] for choice in result["choices"]] == [0, 1]
    assert [choice["message"]["content"] for choice in result["choices"]] == ["A", "B"]
    assert "usage" not in result


@pytest.mark.parametrize(
    ("events", "done", "message"),
    [
        (('{"choices":',), True, "valid JSON"),
        (({"error": {"message": "failed"}},), True, "reported an error"),
        (({"choices": []},), True, "without a choice"),
        (
            (
                {
                    "model": "gpt-5.6-sol",
                    "choices": [{"index": 0, "delta": {"content": "partial"}}],
                },
            ),
            True,
            "finish_reason",
        ),
        (({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},), False, r"without \[DONE\]"),
    ],
)
def test_aggregate_rejects_unproven_terminal_stream(
    events: tuple[object, ...],
    done: bool,
    message: str,
) -> None:
    with pytest.raises(ChatStreamError, match=message):
        _aggregate(*events, done=done)


def test_aggregate_rejects_response_size_overflow() -> None:
    with pytest.raises(ChatStreamError, match="byte limit"):
        _aggregate(
            {"choices": [{"index": 0, "delta": {"content": "long"}, "finish_reason": "stop"}]},
            byte_limit=10,
        )


def test_aggregate_enforces_total_deadline(monkeypatch) -> None:
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(llm_api_stream.time, "monotonic", lambda: next(clock))

    with pytest.raises(ChatStreamError, match="deadline"):
        aggregate_chat_stream(
            _sse(),
            byte_limit=1000,
            timeout=0.1,
            expected_model="gpt-5.6-sol",
        )


def test_aggregate_rejects_chunk_identity_drift() -> None:
    with pytest.raises(ChatStreamError, match="identity changed for model"):
        _aggregate(
            {
                "id": "chat-4",
                "model": "gpt-5.6-sol",
                "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": None}],
            },
            {
                "id": "chat-4",
                "model": "other-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )


def test_aggregate_rejects_response_model_mismatch() -> None:
    with pytest.raises(ChatStreamError, match="model does not match"):
        _aggregate(
            {
                "id": "chat-5",
                "model": "other-model",
                "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"}],
            }
        )


def test_aggregate_rejects_tool_delta_without_index() -> None:
    with pytest.raises(ChatStreamError, match="requires a non-negative index"):
        _aggregate(
            {
                "id": "chat-6",
                "model": "gpt-5.6-sol",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )
