"""Keep evaluator diagnostics when a public workflow raises before returning."""

from __future__ import annotations

import json
import os
import re
import traceback
from pathlib import Path
from typing import Any


def redact_error_text(text: str) -> str:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AZURE_OPENAI_API_KEY", "LLM_API_KEY"):
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)


def record_exception(tracer: Any, error: BaseException) -> str:
    """Preserve the available Python cause chain without replacing the failure."""
    rendered = redact_error_text("".join(traceback.format_exception(error)))
    target = Path(tracer.path).parent / "evaluation-error.json"
    record = {
        "exception_type": type(error).__name__,
        "message": redact_error_text(str(error)),
        "available_traceback": rendered,
        "trajectory_path": str(tracer.path),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    except OSError as write_error:
        tracer.write_error = f"exception evidence write failed ({type(write_error).__name__})"
    return redact_error_text(f"{type(error).__name__}: {error}")


def completed_workflow_usage(path: str) -> dict[str, Any] | None:
    """Read a lower bound from completed events in the reserved workflow trace."""
    source = Path(path)
    if source.name != "orchestration.jsonl":
        return None
    try:
        stream = source.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tokens = completed = started = missing_usage = malformed = duplicates = 0
    seen: set[tuple[str, Any]] = set()
    with stream:
        for line in stream:
            if not line.strip():
                continue
            if "\ufffd" in line:
                malformed += 1
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            kind = event.get("type")
            if kind not in {"llm_call", "llm_call_started"}:
                continue
            sequence = event.get("seq")
            if isinstance(sequence, (str, int)) and not isinstance(sequence, bool):
                identity = (kind, sequence)
                if identity in seen:
                    duplicates += 1
                    continue
                seen.add(identity)
            if kind == "llm_call_started":
                started += 1
                continue
            completed += 1
            metrics = event.get("metrics") or {}
            value = metrics.get("tokens") if isinstance(metrics, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                tokens += value
            else:
                missing_usage += 1
    result = {
        "source": str(source),
        "basis": "completed workflow llm_call events",
        "tokens_known": tokens,
        "completed_model_calls": completed,
        "started_model_calls": started,
        "missing_usage_events": missing_usage,
        "malformed_lines": malformed,
        "duplicate_events_ignored": duplicates,
        "lower_bound": True,
        "unfinished_or_unreported_provider_usage_included": False,
        "steps_basis": "completed model turns; unavailable or unfinished steps excluded",
    }
    target = source.parent / "evaluation-recovered-usage.json"
    try:
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except OSError as write_error:
        result["diagnostic_write_error"] = type(write_error).__name__
    return result
