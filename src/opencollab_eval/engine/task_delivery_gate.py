"""Controller-owned proof that a model received the complete public task."""

from __future__ import annotations

import contextvars
import hashlib
import re
from dataclasses import dataclass

_ACTIVE_DELIVERY: contextvars.ContextVar[TaskDeliveryGate | None] = (
    contextvars.ContextVar("opencollab_eval_task_delivery", default=None)
)
MAX_WORK_BRIEF_BYTES = 512
_TOOL_MARKUP = re.compile(r"</?(?:tool_calls?|invoke)\b", re.IGNORECASE)


def contains_tool_markup(value: str) -> bool:
    return _TOOL_MARKUP.search(value) is not None


@dataclass
class TaskDeliveryGate:
    path: str
    source_sha256: str
    source_text: str
    line_count: int
    interfaces_required: bool
    full_source_delivered: bool = False
    work_brief_bytes: int = 0
    intake_complete: bool = False

    def accept_full_source(self, source_text: str, work_brief: object) -> str:
        if source_text != self.source_text:
            raise ValueError("task intake source does not match controller text")
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("task intake source SHA-256 does not match")
        if not isinstance(work_brief, str) or not work_brief.strip():
            raise ValueError("task intake returned no work brief")
        brief = work_brief.strip()
        if contains_tool_markup(brief):
            raise ValueError("task intake returned tool markup instead of a work brief")
        brief_bytes = len(brief.encode("utf-8"))
        if brief_bytes > MAX_WORK_BRIEF_BYTES:
            raise ValueError(
                f"task intake work brief exceeds {MAX_WORK_BRIEF_BYTES} UTF-8 bytes"
            )
        self.full_source_delivered = True
        self.work_brief_bytes = brief_bytes
        return brief

    def complete_intake(self, work_brief: str) -> str:
        if not self.full_source_delivered or not work_brief:
            raise RuntimeError("task intake ended without complete source delivery")
        self.intake_complete = True
        return (
            f"Public task source {self.source_sha256} was delivered in full to the "
            f"task-intake role.\nWork brief\n{work_brief}\n"
            f"Exact public text remains at {self.path}. Read it when exact wording, "
            "literal values, or interface details are needed."
        )

    def proof(self) -> dict[str, object]:
        return {
            "full_source_delivered": self.full_source_delivered,
            "source_sha256": self.source_sha256,
            "work_brief_bytes": self.work_brief_bytes,
            "interfaces_required": self.interfaces_required,
            "intake_complete": self.intake_complete,
        }


def activate_task_delivery(
    delivery: dict[str, object],
    source_text: str,
) -> contextvars.Token[TaskDeliveryGate | None] | None:
    if delivery.get("delivery") != "git_metadata_file":
        return None
    path = delivery.get("path")
    source_sha256 = delivery.get("source_sha256")
    line_count = delivery.get("delivered_lines")
    interfaces_required = delivery.get("interfaces_required")
    if (
        not isinstance(path, str)
        or not isinstance(source_sha256, str)
        or not isinstance(line_count, int)
        or isinstance(line_count, bool)
        or line_count <= 0
        or not isinstance(interfaces_required, bool)
        or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != source_sha256
    ):
        raise ValueError("staged task delivery is missing controller identity")
    return _ACTIVE_DELIVERY.set(
        TaskDeliveryGate(path, source_sha256, source_text, line_count, interfaces_required)
    )


def reset_task_delivery(token: contextvars.Token[TaskDeliveryGate | None] | None) -> None:
    if token is not None:
        _ACTIVE_DELIVERY.reset(token)


def current_task_delivery() -> TaskDeliveryGate | None:
    return _ACTIVE_DELIVERY.get()


__all__ = [
    "MAX_WORK_BRIEF_BYTES",
    "TaskDeliveryGate",
    "activate_task_delivery",
    "contains_tool_markup",
    "current_task_delivery",
    "reset_task_delivery",
]
