"""Separate message tool attempts from acknowledged sends in saved transcripts."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MESSAGE_TOOL = "message_agent"
MESSAGE_SENT_EVENT = "message_sent"
MESSAGE_REFUSED_EVENT = "message_refused"
_QUEUED_ACK = re.compile(r"Message queued to aid (-?[0-9]+)\.")


def role_key(role: str) -> str:
    """Match case and Unicode spelling variations of an acknowledged role."""
    return unicodedata.normalize("NFC", role.strip()).casefold()


@dataclass
class QueuedReceipt:
    """A successful queue receipt paired with the call that produced it."""

    aid: int
    target: str
    arguments: dict[str, Any]


def message_calls(messages: list[dict[str, Any]]) -> tuple[list[str], list[QueuedReceipt]]:
    """Count calls and recover resolved targets from their matching tool receipts."""
    calls: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    sent: list[QueuedReceipt] = []
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
                    target = f"role:{role_key(role)}"
                else:
                    target = f"aid:{int(match[1])}"
                sent.append(QueuedReceipt(aid=int(match[1]), target=target, arguments=arguments))
    return calls, sent


def event_target(payload: dict[str, Any]) -> str | None:
    """Use the scheduler's resolved role, with its resolved aid as fallback."""
    role = payload.get("to_role")
    if isinstance(role, str) and role:
        return f"role:{role_key(role)}"
    aid = payload.get("to_aid")
    if isinstance(aid, int) and not isinstance(aid, bool):
        return f"aid:{aid}"
    return None


def _same_send(receipt: QueuedReceipt, event: dict[str, Any]) -> bool:
    """Pair a queue decision using its resolved target and recorded payload."""
    aid = event.get("to_aid")
    if isinstance(aid, int) and not isinstance(aid, bool):
        if receipt.aid != aid:
            return False
    elif event_target(event) != receipt.target:
        return False
    arguments = receipt.arguments
    for name in ("summary", "content"):
        value = arguments.get(name)
        length = event.get(f"{name}_chars")
        if length is not None and (not isinstance(value, str) or len(value) != length):
            return False
    summary = event.get("summary")
    if isinstance(summary, str):
        requested = arguments.get("summary")
        if not isinstance(requested, str) or not requested.startswith(summary):
            return False
    content_bytes = event.get("content_bytes")
    if content_bytes is not None:
        content = arguments.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) != content_bytes:
            return False
    return True


def sent_targets(receipts: list[QueuedReceipt], events: list[dict[str, Any]]) -> list[str]:
    """Merge successful decisions and receipts, consuming each match once."""
    remaining = list(receipts)
    successful = [(event, event_target(event)) for event in events if event_target(event) is not None]
    targets = [target for _, target in successful if target is not None]
    # Match detailed decisions before legacy target-only records can consume
    # the receipt needed by a decision with matching message fields.
    fields = ("summary", "summary_chars", "content_chars", "content_bytes")
    for event, _ in sorted(
        successful, key=lambda item: sum(item[0].get(key) is not None for key in fields), reverse=True
    ):
        for index, receipt in enumerate(remaining):
            if _same_send(receipt, event):
                remaining.pop(index)
                break
    targets.extend(receipt.target for receipt in remaining)
    return targets
