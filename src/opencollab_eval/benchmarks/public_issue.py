"""Compose the complete public issue text exposed to a solver."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def compose_public_issue(instance: Mapping[str, Any]) -> str:
    """Join the public Pro fields without exposing grading or identity data."""
    problem = _text(instance.get("problem_statement"))
    sections = [problem] if problem else []
    for heading, field in (
        ("Requirements", "requirements"),
        ("New interfaces introduced", "interface"),
    ):
        value = _text(instance.get(field))
        section = f"{heading}:\n{value}"
        if value.strip() and section not in problem:
            sections.append(section)
    return "\n\n".join(sections)


def solver_public_instance(
    instance: Mapping[str, Any], solver_task_id: str
) -> dict[str, Any]:
    """Build an anonymous external-solver record from public task fields."""
    public = {"repo": instance.get("repo") or "", "instance_id": solver_task_id}
    public["problem_statement"] = compose_public_issue(instance)
    if "hints_text" in instance:
        public["hints_text"] = instance["hints_text"]
    return public
