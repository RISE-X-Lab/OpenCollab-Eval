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

What the text says about cost and about finishing is here for the same reason.
Across seventeen measured runs the median run spent 57% of its budget after the
last edit it made to the source, and seven of them spent half their budget that
way without writing a single byte of documentation: the tail is mostly the same
tests run again after they had already passed, a wider suite run to reconfirm a
pass already held, and a situation rebuilt outside the workspace. Twelve of the
seventeen also tried to download or install something in an environment that
has no network, and two spent an entire budget reading and finished with an
empty tree. None of that is a fact about one arm. It is what the task text left
unsaid -- it named the tests a fix is graded against, and never named the unit
that is scored, what finishing looks like, or what a step costs -- so it belongs
in the text every arm receives.

Two things are deliberate in how it is said. The unscored work is priced, not
prohibited: what a run does with a fixed budget is the behavior being measured,
and a rule that removes the choice removes the measurement with it. And the
budget is described in tokens rather than in steps, because the step ceiling
has never bound a run while the token budget has, and because almost all of
what a step costs is the conversation it re-reads rather than the work it does.

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
The environment has no network access, so a download, an install, or any other
fetch fails and the budget spent on it is gone. Nothing has to be installed,
reinstalled, or built for the project's tests to run.

Do not edit test files: a fix is graded against the project's own tests.

When the run ends, the answer is read from the working tree at /testbed and
from nowhere else. A fix you have worked out but not written into /testbed is
not read at all.

## What is scored

The unit that is scored is the change to the source code: whether the
project's own tests pass against the code left in /testbed. Documentation,
release notes, changelog entries, and code comments are not scored. They are
not forbidden, and a contribution to a real project would carry them, but they
are paid for out of the same budget as the fix.

## When you are finished

You are finished when the change to the source is minimal, addresses the root
cause rather than the symptom, and the project's own tests that cover the
changed behavior pass. Once those tests pass you already hold the result:
running them again, running a wider suite to reconfirm a pass you have, or
rebuilding the situation somewhere outside /testbed all return what you know
already, at full price.

## What a step costs

Your budget is counted in tokens, not in steps; it is limited and it is not
extended. Every step is charged for the whole conversation that came before
it, so each further step costs more than the one before it whatever it does --
reading is not free. Reading a file or searching a term a second time returns
what it returned the first time.

Call submit when you are finished, with a short summary of what you changed.
It ends your turn and records that you stopped on purpose. It does not decide
whether your work counts: /testbed is read either way.
"""

BLIND_VALIDATION_BLOCK = """\
## Blind validation mode
Use only the public issue, repository, tests, and documentation.
"""

#: Prefix of the block ``build_repo_map_via_env`` renders, kept here so the
#: appended section can be recognised without importing the renderer.
REPOSITORY_LAYOUT_HEADER = "## Repository layout"

_CLOSING = (
    "Locate the root cause in the source, apply a minimal fix, and confirm "
    "with the project's own tests that cover it that the behavior described "
    "above is satisfied. Once they pass, the scored work is done; anything "
    "after that is optional and is paid for out of the same budget."
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


def append_repository_layout(task_text: str, repo_map: str) -> str:
    """Put a bounded listing of the workspace at the end of the task text.

    The listing has to be taken through the environment, because the directory
    the agents read is inside the task container and the directory this process
    could walk is the one the run was launched from. That is why it arrives here
    as a string rather than being built here: the two generators reach their
    environment at different points, and only the formatting is shared.

    Sharing the formatting is the part that matters. An arm that is handed a map
    of the repository and an arm that has to go and list it are not doing the
    same task, and neither is an arm whose map is laid out differently. An empty
    ``repo_map`` -- which is what a failed listing returns -- appends nothing, so
    a run without one is a run with a shorter prompt and not a broken one.
    """
    if not repo_map.strip():
        # Say so. A listing that could not be taken produces exactly the same
        # prompt as a run that never asked for one, and that is how this
        # silently produced no listing at all in a container for a whole day:
        # the environment's login shell writes to stderr on every command, the
        # builder read that as a failed traversal, and nothing downstream had a
        # reason to mention it.
        print("  repo map: unavailable — task text has no repository layout")
        return task_text
    return f"{task_text}\n{repo_map.rstrip()}\n"


__all__ = [
    "BLIND_VALIDATION_BLOCK",
    "REPOSITORY_LAYOUT_HEADER",
    "WORKSPACE_FACTS",
    "append_repository_layout",
    "compose_shared_task",
]
