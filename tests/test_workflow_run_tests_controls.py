"""What is left of "a model role cannot choose the test command".

``run_tests`` took a fixed calling convention and refused a runner override or
extra arguments from the model, so a workflow role could not decide *how* the
tests were run. OpenCollab retired the tool, and that control went with it:
every migrated arm runs the project's tests through ``bash``, which by
definition executes whatever command it is given.

What the loss does and does not touch is worth stating, because the control
sounds load-bearing and is not. Scoring never read the agent's own test run --
a batch is graded by the official evaluator against the hidden tests, on the
patch the run produced -- so a role that runs the wrong command still cannot
manufacture a pass. What it can do is *report* one, which is a claim about the
trajectory rather than about the result.

``analyst_solve`` is the arm that did read the agent's evidence, and it is
frozen at its pinned revision rather than migrated; see
``tests/retired_run_tests.py``.
"""

from __future__ import annotations

from importlib import import_module

import pytest

from tests.retired_run_tests import HAVE_RUN_TESTS

MIGRATED = [
    "scout_solve",
    "self_collab",
    "split_solve",
    "swe_committee_v2",
    "validation_council_solve",
]


def _load_workflow(name: str):
    return import_module(f"opencollab_eval.workflows.{name}")


@pytest.mark.parametrize("workflow_name", MIGRATED)
def test_migrated_workflow_roles_run_tests_through_bash(workflow_name):
    """No role holds a test runner any more, and each still holds a shell."""
    module = _load_workflow(workflow_name)

    for factory_name in ("_coder_tools", "_tester_tools"):
        names = [tool.name for tool in getattr(module, factory_name)()]
        assert "run_tests" not in names, (workflow_name, factory_name)
        assert "bash" in names, (workflow_name, factory_name)


@pytest.mark.skipif(
    HAVE_RUN_TESTS, reason="the retired tool is back; re-pin its controls here"
)
def test_the_retired_tool_is_not_reachable_from_this_revision():
    """The lapse itself, so it cannot be reintroduced without a decision.

    OpenCollab's own ``tests/test_bash_test_execution.py`` pins the other half:
    no built-in tool claims a verified test result.
    """
    from opencollab.tools import builtin_tools

    with pytest.raises(ValueError, match="unsupported built-in"):
        builtin_tools("run_tests")
