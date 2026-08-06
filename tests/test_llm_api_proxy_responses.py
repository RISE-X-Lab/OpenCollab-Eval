from __future__ import annotations

import hashlib
import json

from opencollab_eval.commands.llm_api_proxy import (
    _codex_responses_request,
    _responses_request_shape,
)


def test_codex_responses_request_adds_stable_compatibility_fields() -> None:
    source = (
        b'{"model":"gpt-5.6-sol","reasoning":{"effort":"medium"},'
        b'"input":[{"role":"user","content":"task"}]}'
    )
    first_body, first_headers = _codex_responses_request(source)
    second_body, second_headers = _codex_responses_request(source)
    first = json.loads(first_body)
    second = json.loads(second_body)
    assert first["include"] == ["reasoning.encrypted_content"]
    assert first["parallel_tool_calls"] is False
    assert first["text"] == {"verbosity": "low"}
    assert first["reasoning"] == {"effort": "medium", "context": "all_turns"}
    assert len(first["prompt_cache_key"]) == 36
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert len(first["client_metadata"]["session_id"]) == 36
    assert first_headers["x-client-request-id"] != second_headers["x-client-request-id"]
    assert {
        key: value
        for key, value in first_headers.items()
        if key != "x-client-request-id"
    } == {
        key: value
        for key, value in second_headers.items()
        if key != "x-client-request-id"
    }
    assert first_headers["session-id"] == first["client_metadata"]["session_id"]
    assert first_headers["thread-id"] == first["client_metadata"]["thread_id"]
    assert first_headers["x-codex-beta-features"] == "remote_compaction_v2"
    assert len(first_headers["x-client-request-id"]) == 36


def test_codex_responses_request_preserves_explicit_fields() -> None:
    source = json.dumps(
        {
            "input": [],
            "include": [],
            "parallel_tool_calls": True,
            "prompt_cache_key": "caller-key",
            "text": {"verbosity": "high"},
        }
    ).encode()
    body, _headers = _codex_responses_request(source)
    payload = json.loads(body)
    assert payload["include"] == []
    assert payload["parallel_tool_calls"] is True
    assert payload["prompt_cache_key"] == "caller-key"
    assert payload["text"] == {"verbosity": "high"}


def test_codex_responses_request_compacts_only_schema_annotations() -> None:
    source = json.dumps(
        {
            "model": "gpt-5.6-luna",
            "input": [{"role": "user", "content": "task"}],
            "tools": [
                {
                    "type": "function",
                    "name": "file_read",
                    "description": "Read a repository file.",
                    "parameters": {
                        "type": "object",
                        "title": "Read arguments",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Repository path.",
                                "minLength": 1,
                            }
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    ).encode()

    body, _headers = _codex_responses_request(source, compact_tool_schemas=True)

    tool = json.loads(body)["tools"][0]
    assert tool == {
        "type": "function",
        "name": "file_read",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def test_responses_request_shape_contains_no_prompt_or_header_values() -> None:
    body = json.dumps(
        {
            "model": "gpt-5.6-sol",
            "instructions": "private instructions",
            "input": [{"role": "user", "content": "private prompt"}],
            "tools": [{"type": "function", "name": "lookup"}],
            "reasoning": {"effort": "medium"},
        },
        separators=(",", ":"),
    ).encode()
    shape = _responses_request_shape(body, "secret-user-agent")

    assert shape == {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "fields": "input,instructions,model,reasoning,tools",
        "input_bytes": len(
            json.dumps(
                [{"role": "user", "content": "private prompt"}],
                separators=(",", ":"),
            ).encode()
        ),
        "input_items": 1,
        "instructions_bytes": len(json.dumps("private instructions").encode()),
        "largest_tool_bytes": len(
            json.dumps(
                {"type": "function", "name": "lookup"},
                separators=(",", ":"),
            ).encode()
        ),
        "reasoning_effort": "medium",
        "request_bytes": len(body),
        "tools_bytes": len(
            json.dumps(
                [{"type": "function", "name": "lookup"}],
                separators=(",", ":"),
            ).encode()
        ),
        "tools": 1,
        "user_agent_sha256": hashlib.sha256(b"secret-user-agent").hexdigest(),
    }
    assert "private prompt" not in json.dumps(shape)
    assert "private instructions" not in json.dumps(shape)
    assert "secret-user-agent" not in json.dumps(shape)
