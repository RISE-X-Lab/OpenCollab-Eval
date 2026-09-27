"""Duo generation and evaluator capture feed executable local scoring."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys

import pytest
from openai import DefaultAsyncHttpxClient
from opencollab.builtin_workflows import duo, duo_v3
from opencollab.environments import local_environment

from opencollab_eval.engine.evaluator import EvalTask, run_eval_task
from opencollab_eval.engine.python_test_commands import python_test_command
from opencollab_eval.engine.swe_v1_remote_target_proof import fail_to_pass_execution_proof
from opencollab_eval.patch_diff import patch_paths

PUBLIC_TARGET = "test_public.py::test_empty"
HIDDEN_TARGET = "test_hidden.py::test_none"
ORIGINAL = 'def parse(text):\n    return text.split(",")\n'
REPAIR_A = 'def parse(text):\n    return [] if text == "" else text.split(",")\n'
REPAIR_B = 'def parse(text):\n    return [] if not text else text.split(",")\n'


def _git(path, *args, input_text=None):
    return subprocess.run(
        ["git", "-C", str(path), *args], input=input_text,
        check=True, capture_output=True, text=True,
    ).stdout


def _fixture(tmp_path):
    source, scoring = tmp_path / "source", tmp_path / "scoring"
    source.mkdir()
    (source / "widget.py").write_text(ORIGINAL)
    (source / "test_public.py").write_text(
        "from widget import parse\n\ndef test_empty():\n    assert parse('') == []\n"
    )
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(
        source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm",
        "\u6d4b\u8bd5\u521d\u59cb\u6e90\u7801",
    )
    _git(tmp_path, "clone", "-q", str(source), str(scoring))
    (scoring / "test_hidden.py").write_text(
        "from widget import parse\n\ndef test_none():\n    assert parse(None) == []\n"
    )
    return source, scoring


def _score(scoring):
    targets = [PUBLIC_TARGET, HIDDEN_TARGET]
    environment = {
        **os.environ,
        "PATH": str(sys.executable.rsplit("/", 1)[0]) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    execution = subprocess.run(
        ["bash", "-c", python_test_command(targets)], cwd=scoring,
        env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    proof = fail_to_pass_execution_proof(
        {"repo_language": "python"}, targets,
        execution.returncode, execution.stdout + execution.stderr,
    )
    return execution, proof


def _completion(sequence, *, tool_name=None, arguments=None):
    message = {"role": "assistant", "content": "finished" if tool_name is None else None}
    if tool_name is not None:
        message["tool_calls"] = [{
            "index": 0, "id": f"call-{sequence}", "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
        }]
    return {
        "id": f"completion-{sequence}", "object": "chat.completion.chunk", "model": "local-scripted-model",
        "choices": [{"index": 0, "delta": message, "finish_reason": "tool_calls" if tool_name else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _script_model(monkeypatch, source, public_command, *, file_evidence):
    requests = []
    roles = {"A": [], "B": [], "judge": []}
    choice = {
        "winner": "B", "requirements_complete": True,
        "requirements": [{
            "requirement": "Accept None as empty input", "a_coverage": "not_covered", "b_coverage": "covered",
            "a_evidence": ["widget.py still calls text.split on None"],
            "b_evidence": ["widget.py handles None through an empty-list return"],
        }],
        "rationale": "B covers the complete public input requirement in widget.py",
    }

    async def send(client, request, **kwargs):
        transport = importlib.import_module(type(request).__module__.split(".", 1)[0])
        payload = json.loads(request.content)
        requests.append(payload)
        prompt = next(item["content"] for item in payload["messages"] if item["role"] == "user")
        role = "A" if "You are autonomous coder A." in prompt else "B" if "You are autonomous coder B." in prompt else (
            "judge" if "You are the read-only contract adjudicator" in prompt else None
        )
        assert role is not None
        assert (source / "widget.py").read_text() == ORIGINAL
        assert not (source / "test_hidden.py").exists()
        roles[role].append(payload)
        step = len(roles[role])
        if role == "judge":
            if file_evidence and step <= 2:
                label = "A" if step == 1 else "B"
                content = _completion(step, tool_name="read_candidate_evidence",
                                      arguments={"path": f"{label}/candidate.diff"})
            else:
                content = _completion(step, tool_name="structured_output", arguments=choice)
        elif step == 1:
            content = _completion(step, tool_name="file_read", arguments={"path": "widget.py"})
        elif step == 2:
            previous = next(item["content"] for item in reversed(payload["messages"]) if item["role"] == "tool")
            assert "return text.split" in previous
            content = _completion(step, tool_name="file_write", arguments={
                "path": "widget.py", "mode": "create", "content": REPAIR_A if role == "A" else REPAIR_B,
            })
        elif step == 3:
            content = _completion(step, tool_name="bash", arguments={"command": public_command})
        else:
            previous = next(item["content"] for item in reversed(payload["messages"]) if item["role"] == "tool")
            assert "1 passed" in previous
            content = _completion(step)
        if payload.get("stream"):
            encoded = f"data: {json.dumps(content)}\n\ndata: [DONE]\n\n".encode()
            return transport.Response(200, headers={"content-type": "text/event-stream"},
                                      content=encoded, request=request)
        content["object"] = "chat.completion"
        item = content["choices"][0]
        item["message"] = item.pop("delta")
        for call in item["message"].get("tool_calls", []):
            call.pop("index")
        return transport.Response(200, json=content, request=request)

    monkeypatch.setattr(DefaultAsyncHttpxClient, "send", send)
    return requests, roles


@pytest.mark.asyncio
@pytest.mark.parametrize(("workflow", "file_evidence"), [(duo, False), (duo_v3, True)])
async def test_duo_captured_patch_passes_scoring_in_a_fresh_workspace(tmp_path, monkeypatch, workflow, file_evidence):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.delenv("OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT", raising=False)
    source, scoring = _fixture(tmp_path)
    before, failing_proof = _score(scoring)
    assert before.returncode == 1
    assert failing_proof["passed"] == []
    assert failing_proof["failed"] == [PUBLIC_TARGET, HIDDEN_TARGET]
    command = f"{shlex.quote(sys.executable)} -m pytest -q -rA -p no:cacheprovider {PUBLIC_TARGET}"
    requests, roles = _script_model(monkeypatch, source, command, file_evidence=file_evidence)
    environment = local_environment(source)

    async def factory(task):
        return environment

    result = await run_eval_task(
        EvalTask(
            task_id="duo-local-scoring", description="parse must accept empty strings and None as empty input",
            timeout=60, max_tokens=10_000,
            extras={"blind_validation": True, "allow_unisolated_shell": True,
                    "candidate_evidence_dir": str(tmp_path / "evidence")},
        ),
        model="local-scripted-model", provider="openai", api_key="test-key",
        base_url="https://local-scripted.example.invalid/v1", output_dir=str(tmp_path / "output"),
        env_factory=factory, tools_factory=list, workflow=workflow, agent_profile="single2", max_steps=6,
    )
    assert result.error is None, result.error
    assert result.submission_eligible and result.patch_produced
    assert result.workflow_result["winner"] == result.workflow_result["adopted"] == "B"
    assert result.workflow_result["selection_reason"] == "contract-adjudicated"
    assert result.workflow_result["shared_public_command"] == command
    assert patch_paths(result.patch) == ["widget.py"]
    assert (source / "widget.py").read_text() == REPAIR_B
    assert all(roles.values())
    assert command in roles["B"][0]["messages"][1]["content"]
    assert HIDDEN_TARGET not in json.dumps(requests)
    assert "test_hidden.py" not in json.dumps(requests)
    assert result.runtime_state["metrics"]["agent_profile"] == "single2"
    _git(scoring, "apply", "-", input_text=result.patch)
    after, passing_proof = _score(scoring)
    assert after.returncode == 0, after.stdout + after.stderr
    assert passing_proof["ok"] is True
    assert passing_proof["passed"] == [PUBLIC_TARGET, HIDDEN_TARGET]
    assert passing_proof["missing"] == passing_proof["failed"] == []
    assert (scoring / "widget.py").read_text() == REPAIR_B
