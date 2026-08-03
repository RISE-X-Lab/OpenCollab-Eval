"""Bounded, complete delivery of public task specifications to workflows."""

from __future__ import annotations

import hashlib
import json
import uuid

INLINE_TASK_SPECIFICATION_BYTES = 1_500
TASK_SPECIFICATION_JSONL_BYTES = 180
TASK_SPECIFICATION_READ_LINES = 4
TASK_SPECIFICATION_SCHEMA = "opencollab.solver_task_specification.v1"


def _task_specification_line(index: int, text: str) -> str:
    return json.dumps(
        {"i": index, "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _readable_task_specification(description: str) -> str:
    """Encode a public task as reversible JSONL chunks for bounded reads."""
    lines: list[str] = []
    chunk = ""
    for character in description:
        candidate = chunk + character
        line = _task_specification_line(len(lines) + 1, candidate)
        if chunk and len(line.encode("utf-8")) > TASK_SPECIFICATION_JSONL_BYTES:
            lines.append(_task_specification_line(len(lines) + 1, chunk))
            chunk = character
        else:
            chunk = candidate
    if chunk:
        lines.append(_task_specification_line(len(lines) + 1, chunk))
    return "".join(line + "\n" for line in lines)


async def stage_task_description(
    env: object,
    description: str,
) -> tuple[str, dict[str, object]]:
    """Keep short tasks inline and stage long tasks in Solver Git metadata."""
    source = description.encode("utf-8")
    source_sha256 = hashlib.sha256(source).hexdigest()
    if len(source) <= INLINE_TASK_SPECIFICATION_BYTES:
        return description, {
            "schema": TASK_SPECIFICATION_SCHEMA,
            "delivery": "inline",
            "source_bytes": len(source),
            "source_sha256": source_sha256,
        }

    readable = _readable_task_specification(description)
    path = f".git/opencollab-public-task-{uuid.uuid4().hex}.jsonl"
    await env.write_file(path, readable)
    line_count = len(readable.splitlines())
    prompt = (
        f"The complete public task specification is stored in `{path}`. "
        f"Before any repository action, read all {line_count} lines with file_read "
        f"in consecutive chunks of at most {TASK_SPECIFICATION_READ_LINES} lines, "
        "using one file_read call per turn. Each line is JSON with ordered `i` and "
        "`text` fields. Concatenate every `text` value in order without skipping a "
        "chunk. Treat the reconstructed text as the Goal, including every requirement "
        "and interface. "
        f"Its source SHA-256 is {source_sha256}."
    )
    delivered = readable.encode("utf-8")
    return prompt, {
        "schema": TASK_SPECIFICATION_SCHEMA,
        "delivery": "git_metadata_file",
        "source_bytes": len(source),
        "source_sha256": source_sha256,
        "delivered_bytes": len(delivered),
        "delivered_sha256": hashlib.sha256(delivered).hexdigest(),
        "delivered_lines": line_count,
        "path": path,
    }


async def verify_staged_task_description(
    env: object,
    delivery: dict[str, object],
) -> bool:
    """Verify the staged public task after Solver execution has quiesced."""
    if delivery.get("delivery") != "git_metadata_file":
        return True
    path = delivery.get("path")
    expected = delivery.get("delivered_sha256")
    if not isinstance(path, str) or not isinstance(expected, str):
        return False
    try:
        observed = (await env.read_file(path)).encode("utf-8")
    except (OSError, UnicodeError):
        return False
    return hashlib.sha256(observed).hexdigest() == expected


__all__ = ["stage_task_description", "verify_staged_task_description"]
