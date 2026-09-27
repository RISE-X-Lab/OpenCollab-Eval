from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from opencollab_eval.generation import openhands_runtime as runtime


def _install_fake_openhands(monkeypatch: pytest.MonkeyPatch):
    """Install the tiny SDK surface imported by ``install_runtime_overrides``."""
    root = ModuleType("openhands")
    sdk = ModuleType("openhands.sdk")
    sdk_tool = ModuleType("openhands.sdk.tool")
    terminal = ModuleType("openhands.tools.terminal")
    terminal_impl = ModuleType("openhands.tools.terminal.impl")
    terminal_definition = ModuleType("openhands.tools.terminal.definition")
    cli = ModuleType("openhands_cli")
    setup = ModuleType("openhands_cli.setup")
    stores = ModuleType("openhands_cli.stores")
    agent_store_module = ModuleType("openhands_cli.stores.agent_store")

    class FakeToolExecutor:
        pass

    class FakeLLM:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_copy(self, *, update):
            return FakeLLM(**{**self.__dict__, **update})

        def completion(self, *_args, **_kwargs):
            return "completion"

        def responses(self, *_args, **_kwargs):
            return "responses"

    class FakeTerminalTool:
        name = "terminal"

    class FakeObservation:
        @classmethod
        def from_text(cls, **values):
            return SimpleNamespace(**values)

    class FakeAgent:
        def __init__(self):
            self.llm = FakeLLM()
            self.condenser = None
            self.tools = [SimpleNamespace(name="terminal")]

        def model_copy(self, *, update):
            result = FakeAgent()
            result.__dict__.update(self.__dict__)
            result.__dict__.update(update)
            return result

    class FakeAgentStore:
        def load_or_create(self, *_args, **_kwargs):
            return FakeAgent()

    def conversation(*_args, **kwargs):
        return kwargs

    sdk.LLM = FakeLLM
    sdk_tool.ToolExecutor = FakeToolExecutor
    terminal.TerminalTool = FakeTerminalTool
    terminal.impl = terminal_impl
    terminal.definition = terminal_definition
    terminal_definition.TerminalObservation = FakeObservation
    setup.Conversation = conversation
    agent_store_module.AgentStore = FakeAgentStore
    stores.agent_store = agent_store_module
    cli.setup = setup
    root.sdk = sdk
    root.tools = ModuleType("openhands.tools")
    root.tools.terminal = terminal

    modules = {
        "openhands": root,
        "openhands.sdk": sdk,
        "openhands.sdk.tool": sdk_tool,
        "openhands.tools": root.tools,
        "openhands.tools.terminal": terminal,
        "openhands.tools.terminal.impl": terminal_impl,
        "openhands.tools.terminal.definition": terminal_definition,
        "openhands_cli": cli,
        "openhands_cli.setup": setup,
        "openhands_cli.stores": stores,
        "openhands_cli.stores.agent_store": agent_store_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return FakeAgentStore, FakeLLM, terminal_impl, setup


def test_runtime_overrides_rebinds_container_and_settings_per_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AgentStore, _FakeLLM, _terminal_impl, setup = _install_fake_openhands(monkeypatch)
    monkeypatch.setattr(runtime, "_RUNTIME_BINDINGS", runtime._RuntimeBindings())
    monkeypatch.setattr(runtime, "_INSTALLED", False)

    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-a")
    monkeypatch.setenv("OPENHANDS_INSTANCE_ID", "task-a")
    runtime.install_runtime_overrides(
        runtime.RuntimeSettings(temperature=0.1, max_steps=3, token_budget=100)
    )

    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-b")
    monkeypatch.setenv("OPENHANDS_INSTANCE_ID", "task-b")
    runtime.install_runtime_overrides(
        runtime.RuntimeSettings(temperature=0.9, max_steps=7, token_budget=200)
    )

    agent = AgentStore().load_or_create()
    assert agent.llm.temperature == 0.9
    assert setup.Conversation()["max_iteration_per_run"] == 7
    assert runtime._RUNTIME_BINDINGS.container_id == "container-b"
    assert runtime._RUNTIME_BINDINGS.guard is not None
    assert runtime._RUNTIME_BINDINGS.guard.limit == 200


def test_runtime_overrides_can_enable_budget_after_unlimited_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AgentStore, FakeLLM, _terminal_impl, _setup = _install_fake_openhands(monkeypatch)
    monkeypatch.setattr(runtime, "_RUNTIME_BINDINGS", runtime._RuntimeBindings())
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-a")
    monkeypatch.setenv("OPENHANDS_INSTANCE_ID", "task-a")
    runtime.install_runtime_overrides(runtime.RuntimeSettings())
    monkeypatch.setenv("OPENHANDS_INSTANCE_ID", "task-b")
    runtime.install_runtime_overrides(runtime.RuntimeSettings(token_budget=100))

    llm = FakeLLM(max_output_tokens=1)
    llm.metrics = SimpleNamespace(
        accumulated_token_usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    )
    llm.completion()
    assert runtime._RUNTIME_BINDINGS.guard is not None
    assert runtime._RUNTIME_BINDINGS.guard.spent == 0


def test_existing_terminal_executor_uses_latest_container_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _AgentStore, _FakeLLM, terminal_impl, _setup = _install_fake_openhands(monkeypatch)
    monkeypatch.setattr(runtime, "_RUNTIME_BINDINGS", runtime._RuntimeBindings())
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-a")
    runtime.install_runtime_overrides(runtime.RuntimeSettings())
    executor = terminal_impl.TerminalExecutor()

    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-b")
    runtime.install_runtime_overrides(runtime.RuntimeSettings())
    seen: list[str] = []

    def fake_invocation(container_id: str, command: str):
        seen.append(container_id)
        return SimpleNamespace(argv=("fake",), source=command)

    monkeypatch.setattr(runtime, "_guarded_terminal_invocation", fake_invocation)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="ok", stderr="", returncode=0
        ),
    )
    action = SimpleNamespace(reset=False, is_input=False, command="echo ok", timeout=1)
    result = executor(action)

    assert seen == ["container-b"]
    assert result.exit_code == 0


def test_runtime_config_artifact_tracks_latest_reconfiguration(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_openhands(monkeypatch)
    monkeypatch.setattr(runtime, "_RUNTIME_BINDINGS", runtime._RuntimeBindings())
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-a")
    output_dir = tmp_path / "output-a"
    monkeypatch.setenv("OPENHANDS_OUTPUT_DIR", str(output_dir))
    runtime.install_runtime_overrides(runtime.RuntimeSettings(max_steps=3))

    monkeypatch.setenv("OPENHANDS_CONTAINER_ID", "container-b")
    latest_output = tmp_path / "output-b"
    monkeypatch.setenv("OPENHANDS_OUTPUT_DIR", str(latest_output))
    runtime.install_runtime_overrides(
        runtime.RuntimeSettings(max_steps=9, temperature=0.7)
    )

    assert json.loads(
        (latest_output / "runtime_config.json").read_text(encoding="utf-8")
    ) == {
        "context_window": None,
        "temperature": 0.7,
        "top_p": None,
        "max_output_tokens": None,
        "token_budget": None,
        "max_steps": 9,
    }
