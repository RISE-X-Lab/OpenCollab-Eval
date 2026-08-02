from __future__ import annotations

from importlib import import_module

import pytest


def _load_workflow(name: str):
    return import_module(f"opencollab_eval.workflows.{name}")


@pytest.mark.parametrize(
    "workflow_name",
    [
        "analyst_solve",
        "scout_solve",
        "self_collab",
        "split_solve",
        "swe_committee_v2",
        "validation_council_solve",
    ],
)
def test_workflow_model_roles_cannot_override_test_commands(workflow_name):
    module = _load_workflow(workflow_name)

    for factory_name in ("_coder_tools", "_tester_tools"):
        tools = getattr(module, factory_name)()
        if workflow_name == "validation_council_solve" and factory_name == "_coder_tools":
            assert "run_tests" not in {tool.name for tool in tools}
            continue
        run_tests = next(tool for tool in tools if tool.name == "run_tests")
        assert run_tests.allow_runner_override is False
        assert run_tests.allow_extra_args is False
