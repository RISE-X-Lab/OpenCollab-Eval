"""Regression tests for cleanup errors during workflow generation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from gen_prediction_workflow_support import FIXTURE, gpw


def test_generate_preserves_generation_error_when_baseline_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def fail_run_eval_task(task, **kwargs):
        raise RuntimeError("workflow generation failed")

    class FailingBaseline:
        def cleanup(self) -> None:
            raise OSError("baseline cleanup failed")

    finalized = []
    monkeypatch.setattr(gpw, "run_eval_task", fail_run_eval_task)
    monkeypatch.setattr(gpw.gp, "start_container_with_marker", lambda *args: "cid")
    monkeypatch.setattr(gpw.gp, "container_image_id", lambda container_id: "image-id")
    monkeypatch.setattr(
        gpw.gp,
        "prepare_solver_git_snapshot",
        lambda *args: SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        gpw.gp,
        "prepare_trusted_patch_baseline",
        lambda container_id, snapshot: FailingBaseline(),
    )
    monkeypatch.setattr(
        gpw.gp,
        "finalize_container_ownership",
        lambda **kwargs: finalized.append(kwargs),
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
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    with pytest.raises(RuntimeError, match="workflow generation failed") as raised:
        asyncio.run(
            gpw.generate(
                FIXTURE,
                "image",
                cfg,
                args,
                gpw.generate_review_fix,
                "generate_review_fix",
            )
        )

    assert len(finalized) == 1
    assert finalized[0]["completed"] is False
    assert any("baseline cleanup failed" in note for note in raised.value.__notes__)
