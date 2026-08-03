"""Internal workflow opinions cannot veto a verified candidate."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from gen_prediction_workflow_support import FIXTURE, gpw
from gen_prediction_workflow_support import (
    isolated_solver_snapshot as _isolated_solver_snapshot,  # noqa: F401
)
from gen_prediction_workflow_support import trusted_proof as _trusted_proof

from opencollab_eval.engine.evaluator import EvalResult


@pytest.fixture(autouse=True)
def _verified_llm_trajectory(monkeypatch):
    monkeypatch.setattr(
        gpw,
        "_verified_llm_calls",
        lambda _path, *, expected_model, **_kwargs: (
            [expected_model],
            [],
            "a" * 64,
            1,
        ),
    )


def test_provider_failure_remains_an_error_even_when_a_patch_exists():
    result = EvalResult(
        task_id="task-1",
        patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=1.0,
        error="TimeoutError: provider request timed out",
    )

    assert gpw._workflow_status_for_result(result, result.patch) == "error"


def test_workflow_status_preserves_structured_advisory_gap():
    result = EvalResult(
        task_id="task-1",
        patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=1.0,
        workflow_result={"status": "advisory_gap", "done_with_advisory_gap": True},
    )

    assert gpw._workflow_status_for_result(result, result.patch) == "advisory_gap"


@pytest.mark.parametrize("advisory_status", ["blocked", "incomplete", "error"])
def test_internal_review_cannot_block_a_verified_candidate(
    monkeypatch,
    tmp_path,
    advisory_status,
):
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"

    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch=patch,
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={"status": advisory_status},
            error="internal reviewer stopped" if advisory_status == "error" else None,
            runtime_status="completed",
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(gpw.gp, "start_container", lambda image, name, owner_token: "cid")

    def fake_cleanup(run_dir, cid):
        gpw.gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", fake_cleanup)
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: (patch, [], _trusted_proof(patch)),
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(tmp_path / "predictions.jsonl"),
        metrics=str(tmp_path / "metrics.jsonl"),
        model_name="model",
        _persist_output_after_cleanup=True,
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    candidate, metrics = asyncio.run(
        gpw.generate(
            FIXTURE,
            "image",
            cfg,
            args,
            gpw.generate_review_fix,
            "generate_review_fix",
        )
    )

    assert candidate == patch
    assert metrics["workflow_status"] == "done"
    assert metrics["workflow_advisory_status"] == advisory_status
    assert metrics["submission_eligible"] is True
    assert json.loads((tmp_path / "predictions.jsonl").read_text())["model_patch"] == patch
