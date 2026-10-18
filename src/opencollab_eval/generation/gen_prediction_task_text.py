"""Shared public task text and container repository layout for every solver arm."""

from __future__ import annotations

from opencollab_eval.benchmarks.task_specification import (
    CONTAINER_REPO_ROOT,
    compose_task_specification,
)

_WORKSPACE_FACTS_TEMPLATE = """\
## Where you are working

The repository is checked out at {repo_root}. Use its prepared dependencies and
existing toolchain. Follow the environment's actual network and dependency
availability when choosing a check.

Do not edit project test files. Keep the source change in the working tree
at {repo_root}, which is captured as the answer when the run ends.

## What is scored

The unit that is scored is the change to the source code and whether the
project's tests pass against the code left in {repo_root}. Documentation,
release notes, changelog entries, and code comments are not scored. They are
optional work paid for out of the same budget as the fix.

## When you are finished

You are finished when the change to the source is minimal, addresses the root
cause, and the project's own tests that cover the changed behavior pass.
A repeated run of an unchanged passing check returns the same evidence.
Run additional checks when a change, failure, or remaining concern warrants it.

## What a step costs

The configured budget is counted in tokens. Each model call includes the
conversation retained by the runtime, so further steps can consume additional
input and output tokens at their configured cost. Reading is not free.
Reading a file or searching a term a second time returns the same information
when the source is unchanged.

Finish with a short summary of what you changed and the verification result.
The working tree at {repo_root} is captured when the run ends.
"""


def workspace_facts(repo_root: str = CONTAINER_REPO_ROOT) -> str:
    """The machine facts, naming the directory the container presents."""
    return _WORKSPACE_FACTS_TEMPLATE.format(repo_root=repo_root)


WORKSPACE_FACTS = workspace_facts()

BLIND_VALIDATION_BLOCK = """\
## Blind validation mode
Use only the public issue, repository, tests, and documentation.
"""

#: Recognize the repository-map section without importing its renderer.
REPOSITORY_LAYOUT_HEADER = "## Repository layout"

_CLOSING = (
    "Locate the root cause in the source, apply a minimal fix, and confirm "
    "with the project's own tests that cover it that the behavior described "
    "above is satisfied. Once they pass, the scored work is done; anything "
    "after that is optional and is paid for out of the same budget."
)


def compose_shared_task(instance: dict, *, repo_root: str = CONTAINER_REPO_ROOT) -> str:
    """Compose shared task text from public repository, issue, and hint fields.

    Callers append their grading-disclosure block after the closing instruction.
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
        f"{workspace_facts(repo_root)}\n"
        f"{_CLOSING}\n\n"
    )


def append_repository_layout(task_text: str, repo_map: str) -> str:
    """Append a consistently formatted, bounded container-workspace listing.

    Callers obtain the listing through their own environment. An empty repo_map
    appends nothing and leaves the task usable without a repository map.
    """
    if not repo_map.strip():
        print("  repo map: unavailable — task text has no repository layout")
        return task_text
    return f"{task_text}\n{repo_map.rstrip()}\n"


__all__ = [
    "BLIND_VALIDATION_BLOCK",
    "REPOSITORY_LAYOUT_HEADER",
    "WORKSPACE_FACTS",
    "workspace_facts",
    "append_repository_layout",
    "compose_shared_task",
]
