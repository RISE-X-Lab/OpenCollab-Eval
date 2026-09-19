"""Every arm names one directory, and it is the one the container presents.

Images disagree about where a task's repository lives -- SWE-bench Verified
checks out at ``/testbed``, SWE-bench Pro at ``/app`` -- and the driver settles
it before an agent runs, linking whichever directory holds the checkout to
``CONTAINER_REPO_ROOT`` (``gen_prediction_docker.py:195-213``). So the path an
arm's prompt names is a harness fact, not a benchmark fact, and changing
benchmark does not change it.

What can still go wrong is drift between the arms. The path is named thirteen
times across the shared task text and the scripted workflow's own prompts, and
an arm whose copy was edited and another's was not would be describing a
different machine from the arm it is compared against. That difference sits in
the prompt, where no results table can show it -- the shape of every defect the
arm-alignment audit exists for. Hence: one literal, and a check that every arm
follows it when it moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opencollab_eval.benchmarks.task_specification import CONTAINER_REPO_ROOT
from opencollab_eval.generation.gen_prediction_agent import build_task as single_task
from opencollab_eval.generation.gen_prediction_constants import DOCKER_WORKDIR
from opencollab_eval.generation.gen_prediction_task_text import (
    WORKSPACE_FACTS,
    compose_shared_task,
    workspace_facts,
)
from opencollab_eval.generation.gen_prediction_workflow_inputs import (
    build_task as workflow_task,
)
from opencollab_eval.workflows.self_collaboration import (
    ADJUDICATE_PROMPT,
    CODER_PROMPT,
    TESTER_PROMPT,
    reading_analyst_rules,
    shared_rules,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "opencollab_eval"
HOME = SRC / "benchmarks" / "task_specification.py"
#: Modules whose strings are read by a model. A directory literal in one of
#: these is an arm being told about a machine of its own.
PROMPT_MODULES = (
    "generation/gen_prediction_task_text.py",
    "generation/gen_prediction_workflow_inputs.py",
    "generation/gen_prediction_agent.py",
    "workflows/self_collaboration.py",
)
INSTANCE = {"repo": "a/b", "problem_statement": "something is broken"}
#: A root nothing defaults to, used to see whether every arm actually follows
#: the parameter or merely happens to agree with it today.
ELSEWHERE = "/somewhere-else"


def _names(text: str, root: str) -> bool:
    """Whether the text names this directory as a path, not as a substring.

    ``/app`` is a prefix of ``/apply_patch``, which the rules block mentions
    four times, so a plain ``in`` reports every prompt as naming it.
    """
    return re.search(re.escape(root) + r"(?![\w/])", text) is not None


def _dw_prompts(repo_root: str) -> str:
    rules = shared_rules(repo_root)
    parts = [rules, reading_analyst_rules(repo_root)]
    for prompt in (CODER_PROMPT, TESTER_PROMPT, ADJUDICATE_PROMPT):
        parts.append(prompt.replace("{rules}", rules).replace("{repo_root}", repo_root))
    return "\n".join(parts)


def _arm_prompts(repo_root: str | None = None) -> dict[str, str]:
    """Each arm's model-facing text. ``None`` means "however the arm defaults"."""
    if repo_root is None:
        return {
            "single": single_task(INSTANCE),
            "team/workflow": workflow_task(INSTANCE),
            "self-collaboration": _dw_prompts(CONTAINER_REPO_ROOT),
        }
    return {
        "single": single_task(INSTANCE, repo_root=repo_root),
        "team/workflow": workflow_task(INSTANCE, repo_root=repo_root),
        "self-collaboration": _dw_prompts(repo_root),
    }


# --- one directory, and it is the container's ---------------------------------


def test_the_directory_is_written_down_in_exactly_one_place() -> None:
    assert HOME.read_text(encoding="utf-8").count('CONTAINER_REPO_ROOT = "/testbed"') == 1


def test_the_container_is_entered_at_the_directory_the_prompts_name() -> None:
    """Two constants that agree today are two constants. This is one."""
    assert DOCKER_WORKDIR == CONTAINER_REPO_ROOT


@pytest.mark.parametrize("arm", sorted(_arm_prompts()))
def test_every_arm_names_that_directory(arm: str) -> None:
    assert _names(_arm_prompts()[arm], CONTAINER_REPO_ROOT), arm


def test_the_shared_task_text_is_what_every_measured_run_was_given() -> None:
    text = compose_shared_task(INSTANCE)
    assert WORKSPACE_FACTS in text
    assert workspace_facts() == WORKSPACE_FACTS
    assert text.count(CONTAINER_REPO_ROOT) == 6


# --- and every arm follows it when it moves -----------------------------------


@pytest.mark.parametrize("arm", sorted(_arm_prompts()))
def test_no_arm_is_left_behind_when_the_directory_moves(arm: str) -> None:
    """The gate.

    Rendered at a root nothing defaults to, an arm that still names the default
    is an arm carrying its own copy of the path. Today the scripted workflow is
    the one that could: it composes its own prompts rather than the shared task
    text, and it was the one left behind when this was last touched.
    """
    text = _arm_prompts(ELSEWHERE)[arm]
    assert _names(text, ELSEWHERE), (arm, "does not follow the parameter")
    assert not _names(text, CONTAINER_REPO_ROOT), (arm, "still names the default")


@pytest.mark.parametrize("module", PROMPT_MODULES)
def test_no_prompt_module_writes_the_directory_down(module: str) -> None:
    """A second literal is how two arms drift apart with nothing erroring."""
    path = SRC / module
    hits = [
        f"{module}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _names(line, "/testbed") or _names(line, "/app")
    ]
    assert not hits, hits
