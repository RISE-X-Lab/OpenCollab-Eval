"""Retain completed candidates when a caller cancels a public batch."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from opencollab import RunResult

from opencollab_eval.commands.eval_batch import _eval
from opencollab_eval.engine import evaluator, evaluator_sessions


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1-02: batch cancellation loses completed patches")
def test_cli_cancellation_saves_completed_candidate_and_cleans_running_task(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "-C", str(repo), "add", "calc.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "fixture"], check=True,
    )
    task_file = tmp_path / "tasks.jsonl"
    task_file.write_text("".join(
        json.dumps({"task_id": task, "description": task, "repo_path": str(repo)}) + "\n"
        for task in ("first-completed", "second-running")
    ))
    completed = []
    original = evaluator.run_eval_task
    first_result, second_started, second_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        completed.append(result)
        first_result.set()
        return result

    def client_factory(**kwargs):
        env = kwargs["env"]

        class Client:
            async def agent(self, description, **options):
                Path(options["artifacts"], "trajectory.jsonl").write_text('{"type":"completed"}\n')
                await env.write_file("calc.py", "def add(a, b):\n    return a + b\n")
                if description == "second-running":
                    second_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        second_cancelled.set()
                return RunResult(output="done", status="completed", tokens=10,
                                 metrics={"steps": 1, "session_quiesced": True})

        return Client()

    monkeypatch.setattr(evaluator, "run_eval_task", observe)
    monkeypatch.setattr(evaluator_sessions, "_client", client_factory)
    output = tmp_path / "output"

    async def scenario():
        owner = asyncio.create_task(_eval(str(task_file), "stub", "openai", None, None,
                                         str(output), 2, 1000, 30, 0.2))
        await asyncio.wait_for(asyncio.gather(first_result.wait(), second_started.wait()), timeout=5)
        owner.cancel("caller cancellation")
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(scenario())
    assert second_cancelled.is_set()
    assert completed[0].submission_eligible and completed[0].patch_produced
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True) == ""
    result_file = output / "results.jsonl"
    assert result_file.exists()
    records = [json.loads(line) for line in result_file.read_text().splitlines()]
    assert [row["task_id"] for row in records] == ["first-completed"]
    assert records[0]["patch"] == completed[0].patch
    assert records[0]["submission_eligible"] is True
