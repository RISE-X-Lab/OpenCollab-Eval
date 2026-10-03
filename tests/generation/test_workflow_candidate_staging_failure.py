"""Keep completed workflow candidates recoverable after output staging fails."""

from __future__ import annotations

import asyncio
import errno
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.candidate_bytes import CandidateByteLimitError
from opencollab_eval.engine.evaluator_models import EvalResult
from opencollab_eval.generation import gen_prediction_pending as pending
from opencollab_eval.generation import gen_prediction_workflow as wf
from opencollab_eval.generation.gen_prediction_patch import TrustedPatchBaseline
from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot
from tests.support.gen_prediction_workflow_support import FIXTURE, trusted_proof


@pytest.mark.parametrize("failure", ["first_write", "byte_limit", "preservation_required", "candidate_staged"])
def test_first_pending_write_failure_retains_workflow_candidate(monkeypatch, tmp_path, failure):
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    baseline_root = Path(temporary.name)
    (baseline_root / "source.txt").write_text("trusted source\n")
    snapshot = SolverGitSnapshot("b" * 40, "c" * 40, 1, 0, 0, 0, expected_base_commit="a" * 40)
    baseline = TrustedPatchBaseline(snapshot, temporary, baseline_root, "d" * 64, 10, 1, 1)
    runtime = SimpleNamespace(workspace="/testbed", store="/tmp/fixture-runtime", roots=[])
    removed = []
    original_create = pending._atomic_create_bytes

    def disk_full(path, payload):
        if path.parent.name == "pending_outputs" and failure == "first_write":
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        return original_create(path, payload)

    original_replace = pending._replace_owner
    original_persist = wf.gp.persist_pending_output

    def owner_update(path, previous, updated):
        if failure == "preservation_required" and updated["state"] == "candidate_staged":
            raise OSError(errno.EIO, "staged owner update failed")
        return original_replace(path, previous, updated)

    def stage_then_fail(**kwargs):
        original_persist(**kwargs)
        raise OSError(errno.EIO, "staged output returned after write failure")

    if failure == "byte_limit":
        monkeypatch.setattr(pending, "MAX_PENDING_OUTPUT_BYTES", 1)
    elif failure == "candidate_staged":
        monkeypatch.setattr(wf.gp, "persist_pending_output", stage_then_fail)
    monkeypatch.setattr(pending, "_replace_owner", owner_update)

    async def completed(task, **kwargs):
        return EvalResult(task.task_id, patch, True, 1200, 4, 1.0, runtime_status="completed")

    async def repo_map(env):
        return "pkg/a.py"

    def remove_owned(run_dir, cid):
        removed.append(cid)
        wf.gp.clear_container_marker(run_dir, cid)
        return True

    for target, name, value in [
        (wf.gp, "start_container", lambda *a, **k: "fixture-cid"),
        (wf.gp, "_container_owner_label_state", lambda *a: "matching"),
        (wf.gp, "container_image_id", lambda *a: "sha256:" + "8" * 64),
        (wf.gp, "prepare_testbed_environment", lambda *a: None),
        (wf.gp, "stash_solver_runtime_dependencies", lambda *a: runtime),
        (wf.gp, "restore_solver_runtime_dependencies", lambda *a: None),
        (wf.gp, "remove_solver_runtime_dependencies", lambda *a: None),
        (wf.gp, "prepare_solver_git_snapshot", lambda *a: snapshot),
        (wf.gp, "prepare_trusted_patch_baseline", lambda *a: baseline),
        (wf.gp, "remove_container_and_clear_marker", remove_owned),
        (wf, "install_candidate_environment", lambda *a, **k: ""),
        (wf, "attach_container", lambda **k: object()),
        (wf, "build_repo_map_via_env", repo_map),
        (wf, "workflow_model_settings", lambda *a: {"context_window": 8192}),
        (wf, "run_eval_task", completed),
        (wf, "require_container_quiescence", lambda *a: None),
        (wf, "extract_patch_guarded", lambda *a: (patch, [], trusted_proof(patch))),
        (pending, "_atomic_create_bytes", disk_full),
    ]:
        monkeypatch.setattr(target, name, value)
    args = SimpleNamespace(
        timeout=10, budget=2000, max_steps=10, keep_container=False, blind_validation=True,
        checkpoint_interval_seconds=0, resume=False, output=str(tmp_path / "predictions.jsonl"),
        metrics=str(tmp_path / "metrics.jsonl"), model_name="fixture", _persist_output_after_cleanup=True,
    )
    cfg = {"model": "fixture", "provider": "openai", "temperature": 0.0, "thinking": False}

    with pytest.raises((OSError, CandidateByteLimitError)):
        asyncio.run(wf.generate(FIXTURE, "fixture-image", cfg, args, wf.generate_review_fix))

    assert removed == []
    if failure in {"preservation_required", "candidate_staged"}:
        owner_path = next((tmp_path / ".opencollab" / "container_owners").glob("*.json"))
        assert json.loads(owner_path.read_text())["state"] == failure
        staged = next((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
        assert json.loads(staged.read_text())["prediction"]["model_patch"] == patch
        assert not (tmp_path / "candidate-recovery").exists()
        return
    receipt_path = next((tmp_path / "candidate-recovery").glob("*/recovery.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["reason"] == "candidate_record_unwritten"
    assert Path(receipt["baseline"]["git_dir"], "source.txt").read_text() == "trusted source\n"
    owner = json.loads(Path(receipt["owner_record"]).read_text())
    assert owner["state"] == "kept"
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
