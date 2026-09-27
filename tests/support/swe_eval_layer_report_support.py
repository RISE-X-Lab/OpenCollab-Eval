"""Shared preparation for swe eval layer report."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan
from tests.support.generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
    trusted_summary_proof_fields,
)


def _load_module():
    module = importlib.import_module("opencollab_eval.commands.swe_eval_layer_report")
    return importlib.reload(module)



def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path



def _sha(task: str) -> str:
    return hashlib.sha256(task.encode()).hexdigest()



def _direct_summary(task: str, resolved: bool) -> dict:
    f2p_status = 0 if resolved else 1
    target = f"pkg/{task.replace('-', '_')}_test.go::TestCase"
    f2p_plan = prolite_test_plan({"repo_language": "go"}, [target])
    p2p_plan = prolite_test_plan({"repo_language": "go"}, [])
    evidence = {
        "status": f2p_status,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": f2p_status == 0,
        "target_failure_proof_matches_plan": f2p_status != 0,
        "artifact_safe": True,
    }
    candidate_expectation, candidate_projection = candidate_eval_proof_fields(
        task, f"record-{task}", _sha(task)
    )
    return {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "task": task,
        "resolved": resolved,
        "record_id": f"record-{task}",
        "patch_sha256": _sha(task),
        "eval_patch_sha256": _sha(task),
        "eval_image_id": "sha256:" + "9" * 64,
        "filtered_patch_paths": [],
        "eval_spec_sha256": "e" * 64,
        "technical_reasons": [],
        "output_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "candidate_expectation": candidate_expectation,
        "candidate_projection": candidate_projection,
        "source_candidate_projection": candidate_source_projection_fields(candidate_expectation),
        "tests_status": {
            "base_commit_status": 0,
            "service_bootstrap_status": 0,
            "before_repo_status": 0,
            "post_before_base_status": 0,
            "model_patch_status": 0,
            "test_patch_status": 0,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": [evidence],
            "pass_to_pass_evidence": [],
        },
        "report_path": f"/reports/{task}.json",
    }



def _assert_technical(report: dict, reason: str) -> None:
    assert report["counts"]["technical_failed_final"] == 1
    assert report["counts"]["resolved"] == 0
    assert report["counts"]["unresolved"] == 0
    task = report["tasks"][0]
    assert task["resolved"] is None
    assert reason in task["technical_reasons"]



def _row(index: int, task: str, log: str, tokens: int, eval_status: str, resolved=None) -> dict:
    summary = _direct_summary(task, bool(resolved))
    if eval_status != "eval_done":
        summary.update(
            status="technical_eval_failed",
            resolved=False,
            technical_reasons=["fail_to_pass_infra"],
        )
    return {
        "index": index,
        "task": task,
        "generation": {
            "status": "generation_done",
            "task": task,
            "log": log,
            "tokens_used": tokens,
            "steps": 3,
            "duration_s": 20,
            "record_id": f"record-{task}",
            "patch_sha256": _sha(task),
            "eval_patch_sha256": _sha(task),
            "filtered_patch_paths": [],
            **trusted_summary_proof_fields(_sha(task)),
        },
        "eval": {
            "status": eval_status,
            "task": task,
            "executed": eval_status not in {"would_eval", "skipped_empty_patch"},
            "attempt_count": 1,
            "summary": summary,
        },
    }



def _as_verified_empty(row: dict) -> dict:
    row["generation"].update(
        status="empty_patch",
        patch_len=0,
        patch_sha256=hashlib.sha256(b"").hexdigest(),
        workflow_status="empty_patch_after_done",
        submission_integrity="empty_patch_proven",
        submission_eligible=False,
        execution_quiesced=True,
        patch_extraction_succeeded=True,
        injected_path_cleanup_proven=True,
        harness_artifact_exclusion_proven=True,
        checkpoint_restore_integrity_proven=True,
        task_stage_integrity_proven=True,
        test_patch_isolation_failed=False,
        worktree_integrity_proven=True,
        patch_produced=False,
        **trusted_summary_proof_fields(
            hashlib.sha256(b"").hexdigest(),
            patch_bytes=0,
        ),
    )
    row["eval"].update(
        status="skipped_empty_patch",
        executed=False,
        attempt_count=0,
        summary={},
    )
    return row

