"""The task text every arm receives, written once so it cannot drift.

The single-agent path and the workflow/team path each grew their own task
builder, and the two stopped saying the same thing. The workflow one carried
the issue's hint discussion and the single-agent one did not, so an arm was
handed material about where the bug is that the arm it is compared against
never saw. Neither builder stated where the answer is read from, so the model
was left to infer that from the tools it happened to hold.

A difference like that is invisible in a results table and fatal to what the
table is read as. What a run is asked to do, and what it is told about the
machine it is asked to do it on, are not the thing under comparison here; the
way the work is organized is. So both builders compose this text and add only
their own grading-disclosure block on top.

The blind-validation block is here too, as a constant, because the single-agent
path is blind by construction -- it holds no code that can name a sealed field
-- while the workflow path is blind by a check it makes at run time. Same text,
two different reasons it is true, and ``tests/test_gen_prediction_task_text.py``
pins that the two builders agree byte for byte whenever both are blind.
"""

from __future__ import annotations

from opencollab_eval.benchmarks.task_specification import (
    compose_task_specification,
)

WORKSPACE_FACTS = """\
## Where you are working

The repository is checked out at /testbed and its dependencies are installed.

Do not edit test files: a fix is graded against the project's own tests.

When the run ends, the answer is read from the working tree at /testbed and
from nowhere else.
"""

BLIND_VALIDATION_BLOCK = """\
## Blind validation mode
Use only the public issue, repository, tests, and documentation.
"""

_CLOSING = (
    "Locate the root cause in the source, apply a minimal fix, and ensure "
    "the behavior described above is satisfied."
)


def compose_shared_task(instance: dict) -> str:
    """Everything both arms are told, in the order both are told it.

    Reads only public instance fields: the repository name, the issue text, and
    the issue's hint discussion. A caller appends its own grading-disclosure
    block, then this function's closing instruction is already in place above
    it -- so the blocks that differ sit at the end, where a diff of two arms'
    prompts shows exactly what differs and nothing else.
    """
    problem = compose_task_specification(instance)
    hints = (instance.get("hints_text") or "").strip()
    hints_block = (
        f"\n## Hints (from the issue discussion — may help locate the cause)\n{hints}\n"
        if hints
        else ""
    )
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n{hints_block}\n"
        f"{WORKSPACE_FACTS}\n"
        f"{_CLOSING}\n\n"
    )


__all__ = ["BLIND_VALIDATION_BLOCK", "WORKSPACE_FACTS", "compose_shared_task"]
