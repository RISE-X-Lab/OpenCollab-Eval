"""Tests for complete public task delivery under bounded model requests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.environments import local_environment
from opencollab.tools import builtin_tools
from test_gen_prediction_patch import _proof

from opencollab_eval.engine.swe_generation_proof import (
    current_generation_proof_valid,
    current_generation_summary_proof_valid,
)
from opencollab_eval.generation import gen_prediction_workflow as gpw
from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot


def test_long_public_task_is_delivered_completely_through_bounded_reads():
    class Environment:
        def __init__(self):
            self.content = ""
            self.path = ""

        async def write_file(self, path, content):
            self.path = path
            self.content = content

    description = (
        "Issue\n\nRequirements:\n"
        + "every public requirement must remain visible " * 100
        + "\n\nNew interfaces introduced:\n"
        + "func Solve(input string) (Result, error) " * 50
    )
    env = Environment()

    prompt, proof = asyncio.run(gpw._stage_task_description(env, description))

    assert proof["schema"] == "opencollab.solver_task_specification.v1"
    assert proof["delivery"] == "git_metadata_file"
    assert proof["source_sha256"] == gpw.hashlib.sha256(description.encode()).hexdigest()
    assert proof["delivered_sha256"] == gpw.hashlib.sha256(env.content.encode()).hexdigest()
    assert proof["delivered_lines"] == len(env.content.splitlines())
    assert proof["interfaces_required"] is True
    assert proof["path"] == env.path
    assert env.path.startswith(".git/oc-task-")
    assert env.path.endswith(".jsonl")
    assert "controller will deliver all" in prompt
    assert "cumulative task summary" in prompt
    assert "Read the staged file" in prompt
    chunks = [json.loads(line) for line in env.content.splitlines()]
    assert [chunk["i"] for chunk in chunks] == list(range(1, len(chunks) + 1))
    reconstructed = "".join(chunk["text"] for chunk in chunks)
    assert reconstructed.encode() == description.encode()
    assert "Requirements:" in reconstructed
    assert "New interfaces introduced:" in reconstructed


def test_short_public_task_remains_inline_without_writing_a_file():
    class Environment:
        async def write_file(self, *_args, **_kwargs):
            pytest.fail("short public task must remain inline")

    description = "Fix the public API while keeping compatibility."
    prompt, proof = asyncio.run(gpw._stage_task_description(Environment(), description))

    assert prompt == description
    assert proof["delivery"] == "inline"
    assert proof["source_bytes"] == len(description.encode())
    assert proof["interfaces_required"] is False


def test_staged_public_task_must_retain_its_verified_content():
    class Environment:
        content = "trusted task\n"

        async def read_file(self, path):
            assert path == ".git/oc-task-fixture.jsonl"
            return self.content

    delivery = {
        "schema": "opencollab.solver_task_specification.v1",
        "delivery": "git_metadata_file",
        "path": ".git/oc-task-fixture.jsonl",
        "delivered_sha256": gpw.hashlib.sha256(b"trusted task\n").hexdigest(),
    }
    env = Environment()

    assert asyncio.run(gpw._verify_staged_task_description(env, delivery)) is True
    env.content = "changed task\n"
    assert asyncio.run(gpw._verify_staged_task_description(env, delivery)) is False


@pytest.mark.parametrize(
    "special_text",
    [
        "```python\n\tprint('x')  \n```\n",
        "| name | value |\n| --- | --- |\n| α | β |\n",
        "identifier_" + "x" * 300,
        "tabs\tand trailing spaces   ",
        "\u6ca1\u6709\u672b\u5c3e\u6362\u884c",
    ],
)
def test_public_task_jsonl_chunks_are_byte_reversible(special_text):
    description = ("prefix\n" + special_text + "\nsuffix") * 20

    delivered = gpw._readable_task_specification(description)
    reconstructed = "".join(
        json.loads(line)["text"] for line in delivered.splitlines()
    )

    assert reconstructed.encode("utf-8") == description.encode("utf-8")


def test_each_task_chunk_fits_the_real_bounded_file_read(tmp_path: Path):
    description = (("\\\"\t\x00\u4e2d\u6587" * 80) + "\n") * 20
    delivered = gpw._readable_task_specification(description)
    path = tmp_path / ".git" / "oc-task-fixture.jsonl"
    path.parent.mkdir()
    path.write_text(delivered, encoding="utf-8")
    env = local_environment(tmp_path)
    tool = builtin_tools(
        "file_read",
        limits={"file_read": {"max_read_chars": 2048}},
    )[0]
    runtime = SimpleNamespace(environment=env, safety_policy=None)

    output = asyncio.run(
        tool.execute_with_runtime(
            {"path": str(path.relative_to(tmp_path)), "offset": 1, "limit": 1},
            runtime,
        )
    )

    assert "showing 1-1" in output
    assert "[truncated" not in output
    assert len(output) <= 2_048


def test_staged_task_runtime_retains_chunks_until_repository_work_and_restores(monkeypatch):
    source = "source task"
    monkeypatch.setenv("OPENCOLLAB_EAGER_TOOL_KEEP_RECENT", "1")
    monkeypatch.setenv("OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS", "1")
    monkeypatch.setenv(
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS",
        "default=1024,git_diff=4096",
    )
    delivery = {
        "delivery": "git_metadata_file",
        "delivered_lines": 5,
        "path": ".git/oc-task-" + "f" * 32 + ".jsonl",
        "source_sha256": gpw.hashlib.sha256(source.encode()).hexdigest(),
        "interfaces_required": False,
    }

    with gpw._task_delivery_runtime(delivery, source):
        assert os.environ["OPENCOLLAB_EAGER_TOOL_KEEP_RECENT"] == "1"
        assert os.environ["OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS"] == "1"
        assert os.environ["OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"] == (
            "default=640,git_diff=640,file_read=640"
        )

    assert os.environ["OPENCOLLAB_EAGER_TOOL_KEEP_RECENT"] == "1"
    assert os.environ["OPENCOLLAB_HISTORY_KEEP_RECENT_GROUPS"] == "1"
    assert os.environ["OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"] == (
        "default=1024,git_diff=4096"
    )


def test_v2_generation_proof_requires_complete_task_delivery() -> None:
    snapshot = SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 1)
    patch = "diff --git a/a b/a\n"
    metric = {
        "generation_proof_schema": "opencollab.generation_proof.v2",
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": snapshot.as_dict(),
        "trusted_patch_extraction": _proof(snapshot, patch),
        "patch_sha256": gpw.hashlib.sha256(patch.encode()).hexdigest(),
        "solver_task_specification": {
            "schema": "opencollab.solver_task_specification.v1",
            "delivery": "git_metadata_file",
            "source_bytes": 4_000,
            "source_sha256": "d" * 64,
            "interfaces_required": False,
            "delivered_bytes": 4_500,
            "delivered_sha256": "e" * 64,
            "delivered_lines": 34,
            "path": ".git/oc-task-" + "f" * 32 + ".jsonl",
        },
        "solver_task_delivery_gate": {
            "full_source_delivered": True,
            "source_sha256": "d" * 64,
            "work_brief_bytes": 120,
            "interfaces_required": False,
            "intake_complete": True,
        },
    }

    assert current_generation_proof_valid(metric, patch)
    assert current_generation_summary_proof_valid(metric)
    downgraded = dict(metric)
    downgraded.pop("generation_proof_schema")
    downgraded.pop("solver_task_specification")
    downgraded.pop("solver_task_delivery_gate")
    assert not current_generation_proof_valid(downgraded, patch)
    assert not current_generation_summary_proof_valid(downgraded)
    del metric["solver_task_specification"]["delivered_sha256"]
    assert not current_generation_proof_valid(metric, patch)
    assert not current_generation_summary_proof_valid(metric)
