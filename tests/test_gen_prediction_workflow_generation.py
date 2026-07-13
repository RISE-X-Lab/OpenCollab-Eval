"""Tests for workflow generation lifecycle and publication behavior."""

from __future__ import annotations

import asyncio
import json
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


def test_generate_defers_container_patch_extraction(monkeypatch, tmp_path):
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
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
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
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

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
    assert metrics["submission_eligible"] is True
    assert current_generation_proof_valid(metrics, patch)


def test_generate_container_quiescence_failure_prevents_extraction(
    monkeypatch,
    tmp_path,
):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            execution_quiesced=True,
            submission_eligible=True,
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "cid",
    )
    def fail_quiescence(container_id):
        raise RuntimeError("escaped container process")

    monkeypatch.setattr(gpw, "require_container_quiescence", fail_quiescence)
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: pytest.fail("patch extraction must be skipped"),
    )
    finalized = []
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

    with pytest.raises(RuntimeError, match="escaped container process"):
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

    assert finalized[0]["completed"] is False


@pytest.mark.parametrize(
    "result_flags",
    [
        {
            "test_patch_isolation_failed": True,
            "submission_eligible": False,
        },
        {
            "execution_quiesced": False,
            "submission_eligible": False,
        },
        {
            "patch_extraction_succeeded": False,
            "submission_eligible": False,
        },
        {
            "injected_path_cleanup_proven": False,
            "submission_eligible": False,
        },
    ],
    ids=[
        "test-patch-isolation",
        "execution-not-quiesced",
        "internal-extraction-failed",
        "injected-cleanup-unproven",
    ],
)
def test_generate_skips_outer_extraction_for_ineligible_eval_result(
    monkeypatch,
    tmp_path,
    result_flags,
):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="",
            patch_produced=False,
            tokens_used=0,
            steps=0,
            duration=1.0,
            error="harness integrity failure",
            **result_flags,
        )

    finalized = []
    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "cid",
    )
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: pytest.fail("outer patch extraction must be skipped"),
    )
    monkeypatch.setattr(
        gpw.gp,
        "finalize_container_ownership",
        lambda **kwargs: finalized.append(kwargs),
    )
    output = tmp_path / "predictions.jsonl"
    metrics_output = tmp_path / "metrics.jsonl"
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
        metrics=str(metrics_output),
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

    patch, metrics = asyncio.run(
        gpw.generate(
            FIXTURE,
            "image",
            cfg,
            args,
            gpw.generate_review_fix,
            "generate_review_fix",
        )
    )

    assert patch == ""
    assert metrics["submission_eligible"] is False
    assert metrics["patch_produced"] is False
    assert len(finalized) == 1
    assert finalized[0]["cid"] == "cid"
    assert json.loads(output.read_text(encoding="utf-8"))["model_patch"] == ""


def test_generate_persists_completed_patch_only_after_container_cleanup(monkeypatch, tmp_path):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    output = tmp_path / "predictions.jsonl"
    metrics_output = tmp_path / "metrics.jsonl"
    cleanup_observations = []

    def fake_cleanup(run_dir, cid):
        cleanup_observations.append(output.exists())
        gpw.gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", fake_cleanup)
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
        output=str(output),
        metrics=str(metrics_output),
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

    _patch, metrics = asyncio.run(
        gpw.generate(FIXTURE, "image", cfg, args, gpw.generate_review_fix, "generate_review_fix")
    )

    assert metrics["workflow_status"] == "done"
    assert cleanup_observations == [False]
    assert json.loads(output.read_text(encoding="utf-8"))["model_patch"].strip()
    assert json.loads(metrics_output.read_text(encoding="utf-8"))["workflow_status"] == "done"


def test_generate_output_symlink_race_cleans_active_container(monkeypatch, tmp_path):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    output = tmp_path / "predictions.jsonl"
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")

    def race_output_before_staging(*args, **kwargs):
        output.symlink_to(victim)
        patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"
        return patch, [], _trusted_proof(patch)

    monkeypatch.setattr(gpw, "extract_patch_guarded", race_output_before_staging)
    removed = []

    def cleanup_owned_container(run_dir, cid):
        removed.append(cid)
        gpw.gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(
        gpw.gp,
        "remove_container_and_clear_marker",
        cleanup_owned_container,
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=True,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
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

    with pytest.raises(ValueError, match="regular file or absent"):
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

    assert removed == ["cid"]
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert not list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_generate_cleanup_failure_does_not_publish_done(monkeypatch, tmp_path):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    monkeypatch.setattr(
        gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: False
    )
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: (
            "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            [],
            _trusted_proof("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"),
        ),
    )
    output = tmp_path / "predictions.jsonl"
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
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

    with pytest.raises(RuntimeError, match="technical container cleanup failed"):
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

    assert not output.exists()
    assert list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_workflow_status_does_not_relabel_provider_failure_as_timeout_patch():
    result = EvalResult(
        task_id="task-1",
        patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=1.0,
        error="TimeoutError: provider request timed out",
    )

    status = gpw._workflow_status_for_result(result, result.patch)

    assert status == "error"


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

    status = gpw._workflow_status_for_result(result, result.patch)

    assert status == "advisory_gap"


def test_blind_workflow_missing_allowlist_rejects_all_patch_paths(monkeypatch):
    captured = {}

    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={"status": "error"},
        )

    def fake_extract(*args, **kwargs):
        captured.update(kwargs)
        return "", ["pkg/a.py"], _trusted_proof("")

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "cid",
    )
    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: True)
    monkeypatch.setattr(gpw, "extract_patch_guarded", fake_extract)
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=True,
        checkpoint_interval_seconds=0,
        resume=False,
        output="predictions.jsonl",
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    patch, metrics = asyncio.run(
        gpw.generate(
            FIXTURE,
            "image",
            cfg,
            args,
            gpw.generate_review_fix,
            "validation-council-solve",
        )
    )

    assert captured["guard_validation_artifacts"] is True
    assert captured["allowed_paths"] == set()
    assert patch == ""
    assert metrics["workflow_allowlist_missing"] is True


def test_container_marker_survives_failed_remove(monkeypatch, tmp_path):
    gpw.gp.write_container_marker(tmp_path, "cid123", "name123")
    monkeypatch.setattr(gpw.gp, "_remove_owned_container", lambda record: False)

    removed = gpw.gp.remove_container_and_clear_marker(tmp_path, "cid123")

    assert removed is False
    assert (tmp_path / "container.id").read_text(encoding="utf-8") == "cid123\n"
    assert (tmp_path / "container.name").read_text(encoding="utf-8") == "name123\n"


def test_output_records_share_record_id_and_patch_sha():
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"

    prediction, metrics = gpw.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        workflow_name="team-pro",
        record_id="record-1",
    )

    assert prediction["record_id"] == "record-1"
    assert metrics["record_id"] == "record-1"
    assert prediction["patch_sha256"] == gpw._patch_sha256(patch)
    assert metrics["patch_sha256"] == prediction["patch_sha256"]
    assert metrics["instance_id"] == prediction["instance_id"]
    assert prediction["workflow"] == "team-pro"
    assert metrics["workflow"] == "team-pro"
    assert metrics["runner_returncode"] == 0
    assert prediction["workflow_metric"]["runner_returncode"] == 0
