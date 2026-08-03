"""Bounded, complete delivery of public task specifications to workflows."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opencollab_eval.engine.task_delivery_gate import (
    MAX_WORK_BRIEF_BYTES,
    activate_task_delivery,
    contains_tool_markup,
    current_task_delivery,
    reset_task_delivery,
)

INLINE_TASK_SPECIFICATION_BYTES = 1_500
TASK_SPECIFICATION_JSONL_BYTES = 384
TASK_WORKFLOW_TOOL_RESULT_CHARS = 640
TASK_SPECIFICATION_SCHEMA = "opencollab.solver_task_specification.v1"
TASK_INTAKE_BUDGET = 8_000
TASK_INTAKE_FIELDS = ("objective", "requirements", "interfaces", "acceptance")
TASK_INTAKE_FIELD_BYTES = {
    "objective": 80,
    "requirements": 144,
    "interfaces": 168,
    "acceptance": 64,
}
TASK_INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        field: {"type": "string"}
        for field in TASK_INTAKE_FIELDS
    },
    "required": list(TASK_INTAKE_FIELDS),
    "additionalProperties": False,
}
_RUNTIME_ENV_KEYS = (
    "OPENCOLLAB_EAGER_TOOL_KEEP_RECENT",
    "OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS",
    "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS",
)


def _task_specification_line(index: int, text: str) -> str:
    return json.dumps(
        {"i": index, "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def inline_task_specification(description: str) -> dict[str, object]:
    source = description.encode("utf-8")
    return {
        "schema": TASK_SPECIFICATION_SCHEMA,
        "delivery": "inline",
        "source_bytes": len(source),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "interfaces_required": "New interfaces introduced:\n" in description,
    }


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
    identity = inline_task_specification(description)
    source_sha256 = str(identity["source_sha256"])
    interfaces_required = bool(identity["interfaces_required"])
    if len(source) <= INLINE_TASK_SPECIFICATION_BYTES:
        return description, identity

    readable = _readable_task_specification(description)
    path = f".git/oc-task-{uuid.uuid4().hex}.jsonl"
    await env.write_file(path, readable)
    line_count = len(readable.splitlines())
    prompt = (
        f"The complete public task specification is stored in `{path}`. "
        f"The controller will deliver all {line_count} ordered content chunks before "
        "repository work and will carry their cumulative task summary into every working "
        "role. Read the staged file whenever exact wording or interface details are needed. "
        f"Its source SHA-256 is {source_sha256}."
    )
    delivered = readable.encode("utf-8")
    return prompt, {
        "schema": TASK_SPECIFICATION_SCHEMA,
        "delivery": "git_metadata_file",
        "source_bytes": len(source),
        "source_sha256": source_sha256,
        "interfaces_required": interfaces_required,
        "delivered_bytes": len(delivered),
        "delivered_sha256": hashlib.sha256(delivered).hexdigest(),
        "delivered_lines": line_count,
        "path": path,
    }


async def run_task_delivered_workflow(
    ctx: Any,
    args: dict[str, Any],
    workflow_fn: Any,
    source_description: str | None = None,
) -> Any:
    """Deliver the complete task once, then run the workflow with its work brief."""
    delivery = current_task_delivery()
    if delivery is None:
        return await workflow_fn(ctx, args)
    if not isinstance(source_description, str):
        raise RuntimeError("task intake is missing its controller-owned source")
    chunks = [
        json.loads(line)["text"]
        for line in _readable_task_specification(source_description).splitlines()
    ]
    if (
        len(chunks) != delivery.line_count
        or hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
        != delivery.source_sha256
    ):
        raise RuntimeError("task intake source does not match the staged task identity")
    await ctx.phase("task-intake")
    intake = await ctx.agent(
        (
            f"Read the complete public task below. Return the requested structured "
            f"intake in at most {MAX_WORK_BRIEF_BYTES} UTF-8 bytes total. Keep objective "
            "within 80 bytes, requirements within 144, interfaces within 168 and "
            "acceptance within 64. Preserve the "
            "objective, every requirement, public interfaces and acceptance rules. "
            "Use an empty interfaces string only when the task introduces none.\n"
            + source_description
        ),
        label="task-intake",
        tools=[],
        schema=TASK_INTAKE_SCHEMA,
        budget=TASK_INTAKE_BUDGET,
        thinking=False,
    )
    try:
        work_brief = _structured_work_brief(intake, delivery.interfaces_required)
        work_brief = delivery.accept_full_source(source_description, work_brief)
    except ValueError as exc:
        raise RuntimeError("complete task intake is invalid") from exc
    anchor = delivery.complete_intake(work_brief)
    anchored_args = dict(args)
    anchored_args["description"] = anchor
    anchored_args["goal"] = anchor
    return await workflow_fn(ctx, anchored_args)


def _structured_work_brief(intake: object, interfaces_required: bool) -> str:
    if not isinstance(intake, dict) or set(intake) != set(TASK_INTAKE_FIELDS):
        raise ValueError("task intake returned no structured work brief")
    if any(not isinstance(intake[field], str) for field in TASK_INTAKE_FIELDS):
        raise ValueError("task intake returned non-text summary fields")
    values = {field: intake[field].strip() for field in TASK_INTAKE_FIELDS}
    if any(contains_tool_markup(value) for value in values.values()):
        raise ValueError("task intake returned tool markup instead of summary fields")
    if not values["objective"] or not values["requirements"] or not values["acceptance"]:
        raise ValueError("task intake omitted required summary fields")
    if interfaces_required and not values["interfaces"]:
        raise ValueError("task intake omitted required public interfaces")
    brief = "\n".join(
        f"{label} {_clip_utf8(values[field], TASK_INTAKE_FIELD_BYTES[field])}"
        for field, label in (
            ("objective", "Objective"),
            ("requirements", "Requirements"),
            ("interfaces", "Interfaces"),
            ("acceptance", "Acceptance"),
        )
        if values[field]
    )
    if len(brief.encode("utf-8")) > MAX_WORK_BRIEF_BYTES:
        raise ValueError("task intake work brief exceeds its controller budget")
    return brief


def _clip_utf8(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    encoded = compact.encode("utf-8")
    if len(encoded) <= limit:
        return compact
    prefix = encoded[: limit - 3]
    while True:
        try:
            return prefix.decode("utf-8") + "..."
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _bounded_tool_output_limits(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return (
            f"default={TASK_WORKFLOW_TOOL_RESULT_CHARS},"
            f"file_read={TASK_WORKFLOW_TOOL_RESULT_CHARS}"
        )
    if "=" not in raw:
        try:
            default = min(int(raw.strip()), TASK_WORKFLOW_TOOL_RESULT_CHARS)
        except ValueError as exc:
            raise ValueError("tool result limit must be an integer") from exc
        return f"default={default},file_read={TASK_WORKFLOW_TOOL_RESULT_CHARS}"
    entries: list[tuple[str, str]] = []
    seen_default = False
    seen_file_read = False
    for item in raw.split(","):
        name, separator, value = item.strip().partition("=")
        if not separator:
            raise ValueError("tool result limits must use name=value entries")
        try:
            value = str(min(int(value), TASK_WORKFLOW_TOOL_RESULT_CHARS))
        except ValueError as exc:
            raise ValueError("tool result limits must be integers") from exc
        if name == "default":
            seen_default = True
        elif name == "file_read":
            seen_file_read = True
        entries.append((name, value))
    if not seen_default:
        entries.append(("default", str(TASK_WORKFLOW_TOOL_RESULT_CHARS)))
    if not seen_file_read:
        entries.append(("file_read", str(TASK_WORKFLOW_TOOL_RESULT_CHARS)))
    return ",".join(f"{name}={value}" for name, value in entries)


@contextmanager
def task_delivery_runtime(
    delivery: dict[str, object],
    source_description: str,
) -> Iterator[object | None]:
    """Keep only the current raw chunk while the role builds a bounded checklist."""
    if delivery.get("delivery") != "git_metadata_file":
        yield None
        return
    line_count = delivery.get("delivered_lines")
    if not isinstance(line_count, int) or isinstance(line_count, bool) or line_count <= 0:
        raise ValueError("staged task delivery is missing a positive line count")
    previous = {key: os.environ.get(key) for key in _RUNTIME_ENV_KEYS}
    token = activate_task_delivery(delivery, source_description)
    os.environ["OPENCOLLAB_EAGER_TOOL_KEEP_RECENT"] = "1"
    os.environ["OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS"] = "1"
    os.environ["OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"] = _bounded_tool_output_limits(
        previous["OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"]
    )
    try:
        yield current_task_delivery()
    finally:
        reset_task_delivery(token)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


__all__ = [
    "inline_task_specification",
    "run_task_delivered_workflow",
    "stage_task_description",
    "task_delivery_runtime",
    "verify_staged_task_description",
]
