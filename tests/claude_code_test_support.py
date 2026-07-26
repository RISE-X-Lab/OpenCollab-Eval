from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.generation import claude_code_sidecar as ccs

TASK_IMAGE_ID = "sha256:" + "d" * 64
RUNTIME_IMAGE = "registry.example.invalid/claude-runtime:2.1.175"


def write_stream(
    path: Path,
    *,
    model: str = "glm-5.2",
    include_result: bool = True,
    success: bool = True,
    synthetic_api_error: bool = False,
    total_cost_usd: float = 1.25,
) -> None:
    rows = [
        {
            "type": "system",
            "subtype": "init",
            "model": model,
            "claude_code_version": "2.1.175",
            "permissionMode": "dontAsk",
        },
        {"type": "assistant", "message": {"model": model}},
    ]
    if include_result:
        if synthetic_api_error:
            rows.append(
                {
                    "type": "assistant",
                    "message": {
                        "model": "<synthetic>",
                        "content": [
                            {
                                "type": "text",
                                "text": "API Error: Unable to connect to API (ECONNRESET)",
                            }
                        ],
                    },
                }
            )
        rows.append(
            {
                "type": "result",
                "subtype": "success" if success else "error",
                "is_error": not success or synthetic_api_error,
                "stop_reason": (
                    "stop_sequence"
                    if synthetic_api_error
                    else ("end_turn" if success else None)
                ),
                "result": (
                    "API Error: Unable to connect to API (ECONNRESET)"
                    if synthetic_api_error
                    else ""
                ),
                "duration_ms": 100,
                "duration_api_ms": 50,
                "num_turns": 3,
                "total_cost_usd": total_cost_usd,
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": 40,
                },
                "modelUsage": {model: {"inputTokens": 100}},
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def build_sidecar_fixture(
    tmp_path: Path, *, process_returncode: int = 0, **stream_options: object
) -> dict:
    stream = tmp_path / "stream.jsonl"
    settings = tmp_path / "settings.json"
    executable = tmp_path / "claude"
    write_stream(stream, **stream_options)
    ccs.write_settings(settings, "http://127.0.0.1:18788")
    executable.write_bytes(b"fixed claude executable")
    return build_existing_sidecar(tmp_path, process_returncode=process_returncode)


def build_existing_sidecar(tmp_path: Path, *, process_returncode: int = 0) -> dict:
    return ccs.build_sidecar(
        stream_path=tmp_path / "stream.jsonl",
        settings_path=tmp_path / "settings.json",
        executable_path=tmp_path / "claude",
        cli_version_output="2.1.175 (Claude Code)",
        process_returncode=process_returncode,
        runtime_image=RUNTIME_IMAGE,
        runtime_image_id="sha256:" + "a" * 64,
        expected_runtime_image_id="sha256:" + "a" * 64,
        task_image_id=TASK_IMAGE_ID,
        solver_task_id="solver-" + "1" * 32,
        prompt_sha256="2" * 64,
        anonymous_head="3" * 40,
        base_tree="4" * 40,
        raw_patch_sha256="5" * 64,
        candidate_tree="6" * 40,
    )
