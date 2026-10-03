"""Separate message tool attempts from acknowledged sends in saved transcripts."""

from __future__ import annotations

import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Any

MESSAGE_TOOL = "message_agent"
MESSAGE_SENT_EVENT = "message_sent"
MESSAGE_REFUSED_EVENT = "message_refused"
_QUEUED_ACK = re.compile(r"Message queued to aid (-?[0-9]+)\.")
_COMMIT_REF = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_TEAMMATE_PREFIX = re.compile(r"(?:\[(?:Budget|Progress):[^\n]*\n+\s*)*<(teammate-messages?)\s")


def role_key(role: str) -> str:
    """Match case and Unicode spelling variations of an acknowledged role."""
    return unicodedata.normalize("NFC", role.strip()).casefold()


@dataclass
class QueuedReceipt:
    """A successful queue receipt paired with the call that produced it."""

    aid: int
    target: str
    arguments: dict[str, Any]
    tool_call_id: str = ""


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
                sent.append(QueuedReceipt(aid=int(match[1]), target=target, arguments=arguments, tool_call_id=call_id))
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


def received_events(snapshot: dict[str, Any]) -> list[tuple[dict[str, Any], bool]]:
    """Recover queued messages from their recipient's saved envelopes."""
    events = []
    saved = [(message, False) for message in snapshot.get("messages") or []]
    saved.extend((message, True) for message in snapshot.get("pending_messages") or [])
    for message, pending in saved:
        content = message.get("content")
        if message.get("role") != "user" or message.get("kind") == "stop_notice" or not isinstance(content, str):
            continue
        prefix = _TEAMMATE_PREFIX.match(content)
        if prefix is None:
            continue
        closing = f"</{prefix[1]}>"
        end = content.find(closing, prefix.end())
        if end < 0:
            continue
        # Scheduler bodies and attributes are XML-escaped. The literal root
        # closing tag marks the envelope boundary before appended steering.
        start = prefix.start(1) - 1
        try:
            # XML normalizes literal CR and CRLF. Character references preserve
            # the exact scheduler body used by the send receipt and queue metadata.
            envelope_xml = content[start:end + len(closing)].replace("\r", "&#13;")
            root = ET.fromstring(envelope_xml)
        except ET.ParseError:
            continue
        envelopes = [root] if root.tag == "teammate-message" else list(root)
        for envelope in envelopes:
            sender = re.fullmatch(r"A([0-9]+)", envelope.get("teammate_id") or "")
            message_id = envelope.get("message_id")
            if envelope.tag != "teammate-message" or not sender or not message_id:
                continue
            body = "".join(envelope.itertext())
            # The scheduler surrounds the escaped body with exactly one newline.
            if body.startswith("\n") and body.endswith("\n"):
                body = body[1:-1]
            event = {
                "from_aid": int(sender[1]),
                "to_aid": snapshot.get("aid"),
                "to_role": snapshot.get("role"),
                "message_id": message_id,
                "summary": envelope.get("summary"),
                "summary_chars": len(envelope.get("summary") or ""),
                "content": body,
            }
            queued = pending and all(message.get(key) == event[key] for key in ("from_aid", "to_aid", "message_id"))
            queued = queued and all(
                isinstance(message.get(key), int) and not isinstance(message[key], bool)
                for key in ("from_aid", "to_aid")
            )
            queued = queued and message.get("message_content") == body
            events.append((event, queued))
    return events


def _same_send(receipt: QueuedReceipt, event: dict[str, Any]) -> bool:
    """Check compatibility, excluding contradictory identity or body evidence."""
    aid = event.get("to_aid")
    if isinstance(aid, int) and not isinstance(aid, bool):
        if receipt.aid != aid:
            return False
    elif event_target(event) != receipt.target:
        return False
    arguments = receipt.arguments
    call_id = event.get("tool_call_id")
    if isinstance(call_id, str) and call_id and call_id != receipt.tool_call_id:
        return False
    content = event.get("content")
    if isinstance(content, str) and content != arguments.get("content"):
        return False
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
    refs = event.get("commit_refs")
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        requested_refs = list(dict.fromkeys(
            match[0]
            for name in ("summary", "content")
            for match in _COMMIT_REF.finditer(str(arguments.get(name) or ""))
        ))
        # The trace records a bounded prefix and the original distinct count.
        # Compare both so equal sizes cannot merge different commit handoffs.
        if requested_refs[:len(refs)] != refs:
            return False
        found = event.get("commit_refs_found")
        if isinstance(found, int) and not isinstance(found, bool) and len(requested_refs) != found:
            return False
    return True


def sent_targets(receipts: list[QueuedReceipt], events: list[dict[str, Any]]) -> list[str]:
    """Compute the observed send floor with maximum one-to-one overlap."""
    successful = [(event, event_target(event)) for event in events if event_target(event) is not None]
    targets = [target for _, target in successful if target is not None]
    # Match detailed decisions before legacy target-only records can consume
    # the receipt needed by a decision with matching message fields.
    fields = ("summary", "summary_chars", "content_chars", "content_bytes", "commit_refs", "commit_refs_found")
    ordered = sorted(
        successful,
        key=lambda item: (
            bool(item[0].get("tool_call_id")),
            isinstance(item[0].get("content"), str),
            sum(item[0].get(key) is not None for key in fields),
        ),
        reverse=True,
    )
    compatible = [[index for index, receipt in enumerate(receipts) if _same_send(receipt, event)]
                  for event, _ in ordered]
    matched: dict[int, int] = {}
    # Missing identity fields permit overlap, rather than proving identity.
    # An augmenting path prevents an ambiguous row from stranding a compatible
    # receipt and inflating the floor. Exact contradictions stay excluded.
    for start in range(len(ordered)):
        parents: dict[int, tuple[int, int] | None] = {start: None}
        pending = deque([start])
        while pending:
            current = pending.popleft()
            free = next((index for index in compatible[current] if index not in matched), None)
            if free is not None:
                while True:
                    matched[free] = current
                    parent = parents[current]
                    if parent is None:
                        break
                    current, free = parent
                break
            for index in compatible[current]:
                owner = matched[index]
                if owner not in parents:
                    parents[owner] = (current, index)
                    pending.append(owner)
    targets.extend(receipt.target for index, receipt in enumerate(receipts) if index not in matched)
    return targets
