"""Separate message tool attempts from acknowledged sends in saved transcripts."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

MESSAGE_TOOL = "message_agent"
MESSAGE_SENT_EVENT = "message_sent"
MESSAGE_REFUSED_EVENT = "message_refused"
_QUEUED_ACK = re.compile(r"Message queued to aid (-?[0-9]+)\.")


def role_key(role: str) -> str:
    """Match case and Unicode spelling variations of an acknowledged role."""
    return unicodedata.normalize("NFC", role.strip()).casefold()


def message_calls(messages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Count calls and recover resolved targets from their matching tool receipts."""
    calls: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    sent: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or ""
                calls.append(name)
                if name == MESSAGE_TOOL and call.get("id"):
                    try:
                        arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    except ValueError:
                        arguments = {}
                    pending[str(call["id"])] = arguments if isinstance(arguments, dict) else {}
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                continue
            arguments = pending.pop(call_id)
            content = message.get("content")
            match = _QUEUED_ACK.match(content) if isinstance(content, str) else None
            if match:
                # Role requests become usable only after this call's resolved-aid
                # queue receipt. This also supports snapshots missing the target seat.
                role = arguments.get("to_role")
                if isinstance(role, str) and role.strip() and arguments.get("to_aid") is None:
                    sent.append(f"role:{role_key(role)}")
                else:
                    sent.append(f"aid:{int(match[1])}")
    return calls, sent


def event_target(payload: dict[str, Any]) -> str | None:
    """Use the scheduler's resolved role, with its resolved aid as fallback."""
    role = payload.get("to_role")
    if isinstance(role, str) and role:
        return f"role:{role}"
    aid = payload.get("to_aid")
    if isinstance(aid, int) and not isinstance(aid, bool):
        return f"aid:{aid}"
    return None
