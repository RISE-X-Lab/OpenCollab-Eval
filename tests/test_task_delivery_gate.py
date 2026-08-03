from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab import OpenCollab
from opencollab.environments import local_environment

from opencollab_eval.commands.llm_api_stream import streaming_chat_request
from opencollab_eval.engine.task_delivery_gate import TaskDeliveryGate
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.generation.gen_prediction_task_delivery import (
    run_task_delivered_workflow,
    stage_task_description,
    task_delivery_runtime,
)
from opencollab_eval.generation.gen_prediction_workflow import gp
from opencollab_eval.workflows._analyst_solve_defs import SCOPE_PROMPT, SHARED_RULES
from opencollab_eval.workflows._public_api import toolset

_REAL_LONG_PUBLIC_TASK = (
    Path(__file__).parent / "fixtures" / "r118-public-task.txt"
).read_text(encoding="utf-8")


def _response(
    content: str = "ready",
    *,
    tool_calls: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        finish_reason="stop",
        reasoning=None,
        provider_items=[],
        provider_model="deepseek-v4-pro",
    )


class _CaptureOnceLLM:
    def __init__(self):
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def complete(self, messages, tools=None, **_kwargs):
        self.calls.append((messages, tools or []))
        return _response()


class _ScriptedWireLLM:
    def __init__(self):
        self.calls: list[tuple[list[dict], list[dict]]] = []
        self.step = 0

    async def complete(self, messages, tools=None, **_kwargs):
        self.calls.append((messages, tools or []))
        calls: list[tuple[str, dict]] = []
        for index in range(4):
            calls.extend(
                [
                    (
                        "file_read",
                        {"path": "sample.txt", "offset": 1 + index * 10, "limit": 5},
                    ),
                    ("grep", {"pattern": f"line {index}", "path": "sample.txt"}),
                    (
                        "bash",
                        {
                            "command": (
                                "python -c \"import hashlib;"
                                f"print(hashlib.sha256(b'{index}').hexdigest()*100)\""
                            )
                        },
                    ),
                ]
            )
        if self.step >= len(calls):
            return _response("repository evidence gathered")
        name, arguments = calls[self.step]
        self.step += 1
        return _response(
            "",
            tool_calls=[
                {
                    "id": f"call-{self.step:02d}-with-long-provider-identity",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, separators=(",", ":")),
                    },
                }
            ],
        )


class _SummaryContext:
    def __init__(self, work_brief: str = "Preserve the complete public behavior."):
        self.work_brief = work_brief
        self.prompts: list[str] = []

    async def phase(self, title: str) -> None:
        assert title == "task-intake"

    async def agent(self, prompt: str, **kwargs):
        assert kwargs["tools"] == []
        assert kwargs["thinking"] is False
        self.prompts.append(prompt)
        return self.work_brief


def _wire_bytes(messages: list[dict], tools: list[dict]) -> int:
    body = json.dumps(
        {
            "model": "deepseek-v4-pro",
            "messages": messages,
            "tools": tools,
            "temperature": 0.6,
            "top_p": 0.95,
            "max_tokens": 8_192,
            "stream": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    encoded, aggregate, model = streaming_chat_request(
        body,
        compact_tool_schemas=True,
        compact_tool_call_ids=True,
        enable_stream=False,
    )
    assert aggregate is False
    assert model == "deepseek-v4-pro"
    return len(gzip.compress(encoded, mtime=0))


@pytest.mark.parametrize("workflow_name", sorted(gpw._BUNDLED_WORKFLOWS))
def test_task_anchor_sets_goal_and_description_for_every_workflow(workflow_name: str):
    source = "staged task"
    delivery = {
        "delivery": "git_metadata_file",
        "delivered_lines": 1,
        "path": ".git/oc-task-" + "a" * 32 + ".jsonl",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "interfaces_required": False,
    }
    context = _SummaryContext()
    observed = {}

    async def workflow(_ctx, args):
        observed.update(args)
        return workflow_name

    with task_delivery_runtime(delivery, source):
        result = asyncio.run(run_task_delivered_workflow(
            context,
            {"description": "staged task pointer"},
            workflow,
            source,
        ))

    assert result == workflow_name
    assert observed["description"] == observed["goal"]
    assert "was delivered in full" in observed["goal"]


def test_controller_delivers_the_complete_public_task_in_one_intake(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    env = local_environment(tmp_path)
    _prompt, delivery = asyncio.run(stage_task_description(env, _REAL_LONG_PUBLIC_TASK))
    context = _SummaryContext("Keep ServeHTTP and every stated compatibility rule.")

    async def workflow(_ctx, args):
        return args

    with task_delivery_runtime(delivery, _REAL_LONG_PUBLIC_TASK) as gate:
        result = asyncio.run(run_task_delivered_workflow(
            context,
            {"description": "task pointer"},
            workflow,
            _REAL_LONG_PUBLIC_TASK,
        ))
        proof = gate.proof()

    assert len(context.prompts) == 1
    assert _REAL_LONG_PUBLIC_TASK in context.prompts[0]
    assert proof == {
        "full_source_delivered": True,
        "source_sha256": delivery["source_sha256"],
        "work_brief_bytes": len(context.work_brief.encode()),
        "interfaces_required": True,
        "intake_complete": True,
    }
    assert "ServeHTTP" in result["goal"]


def test_controller_binds_complete_source_instead_of_claiming_summary_completeness():
    source = (
        "must preserve auth semantics\n"
        "New interfaces introduced:\nReal.Interface()\n"
        "must update docs"
    )
    gate = TaskDeliveryGate(
        ".git/oc-task-" + "a" * 32 + ".jsonl",
        hashlib.sha256(source.encode()).hexdigest(),
        source,
        2,
        True,
    )
    brief = gate.accept_full_source(source, "Make the code robust")
    anchor = gate.complete_intake(brief)

    assert gate.full_source_delivered is True
    assert gate.source_sha256 in anchor
    assert "Exact public text remains" in anchor
    with pytest.raises(ValueError, match="does not match controller text"):
        gate.accept_full_source(source.replace("auth", "session"), brief)
    with pytest.raises(ValueError, match="512 UTF-8 bytes"):
        gate.accept_full_source(source, "é" * 257)


def test_real_long_task_requests_fit_proxy_limit(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "sample.txt").write_text(
        "\n".join(
            f"line {index} needle "
            + hashlib.sha256(f"line-{index}".encode()).hexdigest()
            for index in range(80)
        ),
        encoding="utf-8",
    )
    env = local_environment(tmp_path)
    _task_prompt, delivery = asyncio.run(stage_task_description(env, _REAL_LONG_PUBLIC_TASK))
    high_entropy_brief = "".join(
        hashlib.sha256(str(index).encode()).hexdigest() for index in range(40)
    )[:512]
    context = _SummaryContext(high_entropy_brief)

    async def workflow(_ctx, args):
        return args

    with task_delivery_runtime(delivery, _REAL_LONG_PUBLIC_TASK):
        anchored = asyncio.run(run_task_delivered_workflow(
            context,
            {"description": "task pointer"},
            workflow,
            _REAL_LONG_PUBLIC_TASK,
        ))

    assert _wire_bytes(
        [
            {"role": "system", "content": gp.WORKFLOW_AGENT_PROMPT},
            {"role": "user", "content": context.prompts[0]},
        ],
        [],
    ) <= 3_400

    repository_llm = _ScriptedWireLLM()

    async def run_repository_role():
        client = OpenCollab(
            tmp_path,
            model="deepseek-v4-pro",
            provider="openai",
            api_key="test-key",
            base_url="http://127.0.0.1:1/v1",
            config={
                "context_window": 34_000,
                "max_output_tokens": 8_192,
                "temperature": 0.6,
                "top_p": 0.95,
            },
            environment=env,
        )
        return await client.agent(
            SCOPE_PROMPT.format(rules=SHARED_RULES, goal=anchored["goal"], target_tests=""),
            system_prompt=gp.WORKFLOW_AGENT_PROMPT,
            tools=toolset(
                "bash",
                "file_read",
                "file_write",
                "apply_patch",
                "grep",
                "run_tests",
                "git_diff",
            ),
            budget=50_000,
            max_steps=16,
            trace=False,
            llm=repository_llm,
        )

    with task_delivery_runtime(delivery, _REAL_LONG_PUBLIC_TASK):
        result = asyncio.run(run_repository_role())
    assert result.status == "completed"
    assert len(repository_llm.calls) == 13
    wire_sizes = [
        _wire_bytes(messages, tools)
        for messages, tools in repository_llm.calls
    ]
    assert max(wire_sizes) <= 3_200, wire_sizes
