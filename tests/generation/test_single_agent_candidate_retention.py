"""Retain a single-agent candidate after ordinary execution failures."""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import tempfile
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.generation import gen_prediction_docker as owners
from opencollab_eval.generation.gen_prediction_patch import TrustedPatchBaseline
from tests.generation.test_gen_prediction_single_agent_recovery import _trusted_extraction


@pytest.fixture
def single_candidate(tmp_path, monkeypatch):
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({
        "instance_id": "task-1", "repo": "fixture/repo", "base_commit": "c" * 40,
        "problem_statement": "repair the candidate",
    }))
    output = tmp_path / "out/predictions.jsonl"
    workspace = tmp_path / "candidate.patch"
    roots = []
    removed = []
    snapshot = gp.SolverGitSnapshot(
        anonymous_head="a" * 40, base_tree="b" * 40, commit_count=1,
        remote_count=0, extra_git_metadata=0, removed_git_metadata=0,
    )

    def baseline(_cid, _snapshot):
        temporary = tempfile.TemporaryDirectory(prefix="single-retention-", dir=tmp_path)
        root = Path(temporary.name)
        (root / "trusted-before-solver").write_text("original baseline")
        roots.append(root)
        return TrustedPatchBaseline(
            snapshot=snapshot, temporary_directory=temporary, git_dir=root / "repo.git",
            archive_sha256="a" * 64, archive_bytes=1, archive_entries=1, extracted_bytes=1,
        )

    def start(_image, name, run_dir):
        owners.write_container_marker(run_dir, "fixture-cid", name)
        return "fixture-cid"

    def remove(record):
        removed.append(record["container_id"])
        workspace.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(sys, "argv", ["generator", "--instance-file", str(instance), "--output", str(output)])
    monkeypatch.setattr(gp, "get_config", lambda _root, **_kwargs: {"model": "model", "provider": "provider"})
    monkeypatch.setattr(gp, "start_container_with_marker", start)
    monkeypatch.setattr(gp, "container_image_id", lambda _cid: "sha256:" + "8" * 64)
    monkeypatch.setattr(gp, "prepare_testbed_environment", lambda _cid: None)
    monkeypatch.setattr(gp, "stash_solver_runtime_dependencies", lambda *_args: object())
    monkeypatch.setattr(gp, "restore_solver_runtime_dependencies", lambda *_args: None)
    monkeypatch.setattr(gp, "remove_solver_runtime_dependencies", lambda *_args: None)
    monkeypatch.setattr(gp, "prepare_solver_git_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(gp, "prepare_trusted_patch_baseline", baseline)
    monkeypatch.setattr(gp, "require_container_quiescence", lambda _cid: None)
    monkeypatch.setattr(gp, "run_with_bounded_shutdown", lambda awaitable: asyncio.run(awaitable))
    monkeypatch.setattr(gp, "_container_owner_label_state", lambda *_args: "matching")
    monkeypatch.setattr(owners, "_remove_owned_container", remove)
    return output, workspace, roots, removed


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
def test_single_agent_interrupt_preserves_candidate_and_trusted_baseline(single_candidate, monkeypatch, error_type):
    output, workspace, roots, removed = single_candidate

    async def interrupted_agent(*_args, **_kwargs):
        owner_paths = list((output.parent / ".opencollab/container_owners").glob("*.json"))
        assert owners._read_owner(owner_paths[0])["state"] == "kept"
        workspace.write_text("diff --git a/a b/a\n+in progress\n")
        raise error_type("interrupted after editing")

    monkeypatch.setattr(gp, "run_agent", interrupted_agent)
    with pytest.raises(error_type, match="interrupted after editing"):
        gp.main()
    gc.collect()
    assert workspace.exists()
    assert removed == []
    receipt_paths = list((output.parent / "candidate-recovery").glob("*/recovery.json"))
    assert len(receipt_paths) == 1
    receipt = json.loads(receipt_paths[0].read_text())
    assert receipt["generation_error_type"] == error_type.__name__
    assert receipt["reason"] == "trusted_patch_extraction_incomplete"
    assert Path(receipt["baseline"]["git_dir"]).parent.joinpath("trusted-before-solver").read_text() == (
        "original baseline"
    )
    assert not roots[0].exists()
    assert owners._read_owner(Path(receipt["owner_record"]))["state"] == "kept"
    assert not output.exists()


def test_single_agent_model_preparation_failure_cleans_its_empty_source(single_candidate, monkeypatch):
    output, workspace, roots, removed = single_candidate

    def unavailable_runtime(*_args):
        raise OSError("runtime restoration failed")

    monkeypatch.setattr(gp, "restore_solver_runtime_dependencies", unavailable_runtime)
    with pytest.raises(OSError, match="runtime restoration failed"):
        gp.main()
    assert removed == ["fixture-cid"]
    assert not roots[0].exists()
    assert not workspace.exists()
    assert not list((output.parent / ".opencollab/container_owners").glob("*.json"))
    assert not (output.parent / "candidate-recovery").exists()


def test_single_agent_ineligible_result_preserves_unextracted_source(single_candidate, monkeypatch):
    output, workspace, _roots, removed = single_candidate

    async def failed_agent(*_args, **_kwargs):
        workspace.write_text("candidate made before cleanup failed")
        return {"workflow_status": "error", "candidate_probe_eligible": False, "session_quiesced": False}

    monkeypatch.setattr(gp, "run_agent", failed_agent)
    with pytest.raises(SystemExit) as stopped:
        gp.main()
    assert stopped.value.code == 1
    assert workspace.exists()
    assert removed == []
    receipts = list((output.parent / "candidate-recovery").glob("*/recovery.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["reason"] == "trusted_patch_extraction_incomplete"
    prediction = json.loads(output.read_text())
    assert prediction["model_patch"] == ""
    assert prediction["workflow_metric"]["submission_eligible"] is False


def test_single_agent_isolates_retained_source_when_quiescence_fails(single_candidate, monkeypatch):
    _output, workspace, _roots, removed = single_candidate
    isolated = []

    async def interrupted_agent(*_args, **_kwargs):
        workspace.write_text("candidate before interruption")
        raise KeyboardInterrupt("interrupted after editing")

    def unavailable_quiescence(_cid):
        raise RuntimeError("container process quiescence unavailable")

    monkeypatch.setattr(gp, "run_agent", interrupted_agent)
    monkeypatch.setattr(gp, "require_container_quiescence", unavailable_quiescence)
    monkeypatch.setattr(gp, "isolate_container_for_preservation", lambda cid: isolated.append(cid) or "paused")
    with pytest.raises(KeyboardInterrupt, match="interrupted after editing"):
        gp.main()
    assert isolated == ["fixture-cid"]
    assert workspace.exists()
    assert removed == []


def test_single_agent_keeps_existing_pending_transaction_after_owner_write_failure(single_candidate, monkeypatch):
    output, workspace, _roots, removed = single_candidate
    patch = "diff --git a/a b/a\n+completed candidate\n"

    async def completed_agent(*_args, **_kwargs):
        workspace.write_text(patch)
        return {"workflow_status": "done", "candidate_probe_eligible": True, "session_quiesced": True}

    replace = gp._replace_owner

    def unavailable_owner_upgrade(path, previous, updated):
        if updated["state"] == "candidate_staged":
            raise OSError("owner upgrade failed")
        replace(path, previous, updated)

    monkeypatch.setattr(gp, "run_agent", completed_agent)
    monkeypatch.setattr(gp, "extract_patch_guarded", lambda *_args: (patch, [], _trusted_extraction(patch).as_dict()))
    monkeypatch.setattr(gp, "_replace_owner", unavailable_owner_upgrade)
    with pytest.raises(OSError, match="owner upgrade failed"):
        gp.main()
    assert workspace.exists()
    assert removed == []
    owner_paths = list((output.parent / ".opencollab/container_owners").glob("*.json"))
    assert owners._read_owner(owner_paths[0])["state"] == "preservation_required"
    assert len(list((output.parent / ".opencollab/pending_outputs").glob("*.json"))) == 1
    assert not (output.parent / "candidate-recovery").exists()
