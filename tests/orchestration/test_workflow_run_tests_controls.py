"""Workflow roles execute tests through Bash and the OCE verification adapters."""

from __future__ import annotations

from importlib import import_module

import pytest

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


def test_the_retired_tool_is_not_reachable_from_this_revision():
    """The lapse itself, so it cannot be reintroduced without a decision.

    OpenCollab's own ``tests/test_bash_test_execution.py`` pins the other half:
    no built-in tool claims a verified test result.
    """
    from opencollab.tools import builtin_tools

    with pytest.raises(ValueError, match="unsupported built-in"):
        builtin_tools("run_tests")
