"""Track incomplete model calls from native trajectory events per role."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _call_fields(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    aid = payload.get("aid")
    session_step = payload.get("session_step")
    session_id = payload.get("response_session_id")
    role = payload.get("role")
    timestamp = row.get("timestamp")
    if (
        not isinstance(aid, int)
        or not isinstance(session_step, int)
        or session_step < 0
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(timestamp, (int, float))
    ):
        return None
    identity = [source, aid, session_step, session_id]
    return {
        "source": source,
        "aid": aid,
        "role": role,
        "session_step": session_step,
        "response_session_id": session_id,
        "call_identity": identity,
        "event_epoch": float(timestamp),
    }


def _key(identity: list[Any]) -> str:
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def update_events(
    events_by_source: dict[str, list[dict[str, Any]]],
    previous: dict[str, Any] | None,
    *,
    now: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    pending = dict((previous or {}).get("pending_calls") or {})
    completed = []
    for source, rows in events_by_source.items():
        for row in rows:
            fields = _call_fields(row, source)
            if fields is None:
                continue
            key = _key(fields["call_identity"])
            if row.get("type") == "llm_call_started":
                prior = pending.get(key)
                if prior is None:
                    pending[key] = {
                        **fields,
                        "started_epoch": fields["event_epoch"],
                    }
                else:
                    prior["started_epoch"] = min(float(prior["started_epoch"]), fields["event_epoch"])
            elif row.get("type") == "llm_call_cancelled":
                matched = pending.pop(key, None)
                if matched is not None:
                    completed.append(
                        {
                            **fields,
                            "started_epoch": matched["started_epoch"],
                            "cancelled_epoch": fields["event_epoch"],
                            "cancelled": True,
                        }
                    )
            elif row.get("type") == "llm_call" and {
                "content",
                "tool_calls",
                "finish_reason",
            }.issubset(row.get("payload") or {}):
                matched = pending.pop(key, None)
                if matched is not None:
                    completed.append(
                        {
                            **fields,
                            "started_epoch": matched["started_epoch"],
                            "completed_epoch": fields["event_epoch"],
                        }
                    )
    pause_candidates = []
    for value in pending.values():
        elapsed = max(0.0, now - float(value["started_epoch"]))
        value["elapsed_seconds"] = elapsed
        if elapsed >= timeout_seconds:
            pause_candidates.append(
                {
                    **value,
                    "reason": "role_model_call_has_no_complete_event",
                    "action": "pause_whole_task_and_preserve_role_session",
                }
            )
    pause_candidates.sort(key=lambda item: (item["started_epoch"], item["source"], item["aid"]))
    return {
        "observed_epoch": now,
        "timeout_seconds": timeout_seconds,
        "pending_calls": pending,
        "completed_calls": completed,
        "pause_candidates": pause_candidates,
        "event_basis": ("llm_call_started paired with completed llm_call by source aid "
                        "session_step and response_session_id"),
    }


def _incremental(path: Path, cursor: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    rows = []
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        identity = [stat.st_dev, stat.st_ino]
        reset = cursor.get("identity") != identity
        offset = 0 if reset else int(cursor.get("offset", 0))
        if offset > stat.st_size:
            offset = 0
            reset = True
        stream.seek(offset)
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                return rows, {"identity": identity, "offset": position}, reset
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)


def scan(
    paths: list[str | Path],
    previous: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    timeout_seconds: float = 43200.0,
) -> dict[str, Any]:
    previous = previous or {}
    offsets = dict(previous.get("offsets") or {})
    pending = dict(previous.get("pending_calls") or {})
    events: dict[str, list[dict[str, Any]]] = {}
    errors = []
    for raw in paths:
        path = Path(raw)
        source = str(path)
        try:
            rows, cursor, reset = _incremental(path, offsets.get(source) or {})
            offsets[source] = cursor
            if reset:
                pending = {key: value for key, value in pending.items() if value.get("source") != source}
            events[source] = rows
        except (OSError, ValueError, TypeError) as error:
            errors.append({"source": source, "error_type": type(error).__name__})
    result = update_events(
        events,
        {"pending_calls": pending},
        now=time.time() if now is None else now,
        timeout_seconds=timeout_seconds,
    )
    result.update(offsets=offsets, errors=errors)
    return result


__all__ = ["scan", "update_events"]
