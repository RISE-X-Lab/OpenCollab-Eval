"""Workflow generation resolves an independent agent profile and records it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from gen_prediction_workflow_support import (
    FIXTURE,
    gpw,
)
from gen_prediction_workflow_support import (
    isolated_solver_snapshot as _isolated_solver_snapshot,  # noqa: F401
)
from gen_prediction_workflow_support import (
    trusted_proof as _trusted_proof,
)

from opencollab_eval.engine.evaluator import EvalResult
from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid
from opencollab_eval.runtime_config import resolve_runtime_config


def _assert_generate_defers_container_patch_extraction(monkeypatch, tmp_path, agent_profile, cfg_profile=None):
    captured = {}
    async def fake_run_eval_task(task, **kwargs):
        captured["task"] = task
        captured["kwargs"] = kwargs
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={"allowed_patch_paths": ["pkg/a.py"]},
            patch_extraction_succeeded=False,
            submission_eligible=False,
        )
    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token, **kwargs: "cid"
    )
    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: True)
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: (
            "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            [],
            _trusted_proof("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"),
        ),
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
        agent_profile=agent_profile,
    )
    cfg = resolve_runtime_config(
        tmp_path,
        overrides={
            "model": "m",
            "provider": "openai",
            "api_key": "fixture-" + "A" * 24,
            "base_url": "https://model.example.invalid/v1",
            "temperature": 0.0,
            "thinking": False,
        },
    )
    cfg["agent_profile"] = cfg_profile

    patch, metrics = asyncio.run(
        gpw.generate(FIXTURE, "image", cfg, args, gpw.generate_review_fix, "generate_review_fix")
    )

    assert patch.strip()
    assert metrics["checkpoint_result"] is None
    assert metrics["solver_git_snapshot"]["commit_count"] == 1
    assert captured["task"].task_id == "solver-opaque-test-id"
    assert FIXTURE["instance_id"] not in captured["task"].task_id
    assert captured["kwargs"]["checkpoint_interval_seconds"] is None
    assert captured["kwargs"]["resume_from_checkpoint"] is False
    assert captured["kwargs"]["defer_patch_extraction"] is True
    expected_profile = agent_profile if agent_profile is not None else cfg_profile
    assert captured["kwargs"]["agent_profile"] == expected_profile
    if expected_profile is not None:
        assert metrics["agent_profile"] == expected_profile
    else:
        assert "agent_profile" not in metrics
    assert captured["kwargs"]["prompt"] == gpw.gp.WORKFLOW_AGENT_PROMPT
    assert "Obey the current software role" in captured["kwargs"]["prompt"]
    assert "public repository evidence only" in captured["kwargs"]["prompt"]
    assert "you MUST edit" not in captured["kwargs"]["prompt"]
    assert "api_key" not in captured["kwargs"]
    assert "base_url" not in captured["kwargs"]
    assert metrics["llm_base_url_sha256"] == gpw.hashlib.sha256(
        b"https://model.example.invalid/v1"
    ).hexdigest()
    assert metrics["submission_eligible"] is True
    assert current_generation_proof_valid(metrics, patch)
    assert "path_audit" not in metrics["trusted_patch_extraction"]
    assert metrics["patch_path_audit"] == {
        "actual_paths": ["pkg/a.py"],
        "selection_policy": "all_changes_against_verified_baseline",
    }



@pytest.mark.parametrize(
    ("selected_profile", "configured_profile"),
    [(None, None), ("single2", None), (None, "single2")],
)
def test_workflow_generation_resolves_and_records_agent_profile(
    monkeypatch, tmp_path, selected_profile, configured_profile,
):
    _assert_generate_defers_container_patch_extraction(monkeypatch, tmp_path, selected_profile, configured_profile)
