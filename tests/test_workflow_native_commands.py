"""Research workflow factories expose native command schemas after tool removal."""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from opencollab.tools import builtin_tools
from test_native_bash_evidence import COMMAND, TARGET, Environment, runtime

from opencollab_eval import workflows
from opencollab_eval.workflows.validation_council_g20_coder_red_green_v2 import _run_mechanical_probe


def test_all_workflow_tool_factories_use_native_bash_schema():
    native_schema = builtin_tools("bash")[0].to_openai_schema()
    checked = set()
    for item in pkgutil.iter_modules(workflows.__path__):
        module = importlib.import_module(f"{workflows.__name__}.{item.name}")
        for name, factory in vars(module).items():
            if not name.endswith("_tools") or not inspect.isfunction(factory):
                continue
            try:
                inspect.signature(factory).bind()
            except TypeError:
                continue
            tools = factory()
            if not isinstance(tools, (list, tuple)):
                continue
            for tool in tools:
                assert tool.name != "run_tests", (item.name, name)
                if tool.name == "bash":
                    assert tool.to_openai_schema() == native_schema
            checked.add(item.name)
    assert {"validation_council_solve", "base_team", "scout_solve", "self_collab", "split_solve"} <= checked


class MechanicalContext:
    def __init__(self, environment):
        self.environment = environment
        self.calls = []

    async def execute_verification(self, tool, params):
        self.calls.append((tool.name, params))
        return await tool.execute_with_runtime(params, runtime(self.environment))

    async def log(self, message):
        pass


async def test_mechanical_replay_executes_exact_recorded_native_command():
    env = Environment()
    ctx = MechanicalContext(env)
    reference = {"target": TARGET, "runner": "python -m pytest", "command": COMMAND}
    records, error = await _run_mechanical_probe(ctx, reference)
    assert error is None
    assert env.calls == [(COMMAND, 300.0)]
    assert ctx.calls == [("bash", {"command": COMMAND, "timeout": 300.0})]
    assert records == [{**reference, "exit_code": 0, "verified": True}]


@pytest.mark.parametrize("problem", ["target", "runner", "sandbox"])
async def test_mechanical_replay_retains_target_identity_and_isolation(problem):
    env = Environment()
    reference = {"target": TARGET, "runner": "python -m pytest", "command": COMMAND}
    if problem == "target":
        reference["target"] = "different/test.py"
    elif problem == "runner":
        reference["runner"] = "pytest"
    else:
        env.process_isolated = False
    records, error = await _run_mechanical_probe(MechanicalContext(env), reference)
    assert records == []
    assert error
    if problem == "sandbox":
        assert not env.calls
