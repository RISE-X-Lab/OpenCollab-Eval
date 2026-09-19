"""Compose the complete benchmark task specification exposed to a solver."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


#: Where every container presents the task's repository, on every benchmark.
#:
#: It is not a fact about SWE-bench Verified, which is how it reads. Images
#: differ -- Verified checks out at ``/testbed``, SWE-bench Pro at ``/app`` --
#: and the driver normalizes that away before an agent runs: its prepare step
#: links whichever directory holds the checkout to this path
#: (``gen_prediction_docker.py:195-213``), so an arm never has to know which
#: benchmark it is on.
#:
#: Written down once, here, because the arms' prompts all name it. Two literals
#: is how one arm ends up describing a different machine from the arm it is
#: compared against -- a difference that lives in the prompt, where no results
#: table can show it.
CONTAINER_REPO_ROOT = "/testbed"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def compose_task_specification(instance: Mapping[str, Any]) -> str:
    """Join every solver-visible Pro task field without exposing sealed data."""
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


def solver_task_instance(
    instance: Mapping[str, Any], solver_task_id: str
) -> dict[str, Any]:
    """Build an anonymous external-solver record with the complete task."""
    public = {"repo": instance.get("repo") or "", "instance_id": solver_task_id}
    public["problem_statement"] = compose_task_specification(instance)
    if "hints_text" in instance:
        public["hints_text"] = instance["hints_text"]
    return public
