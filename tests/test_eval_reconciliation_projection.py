from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
)
from test_swe_eval_layer_report import _row

from opencollab_eval.commands.swe_eval_layer_report import build_report
from opencollab_eval.commands.swe_v1_prolite_report import (
    eval_only_reconciliation_reports,
)


def _bound_row(
    index: int,
    task: str,
    status: str,
    resolved: bool,
    eval_sha: str,
    paths: list[str],
) -> dict:
    row = _row(index, task, f"/run/{index}-{status}.log", 10, status, resolved)
    source_sha = row["generation"]["patch_sha256"]
    record_id = row["generation"]["record_id"]
    expectation, projection = candidate_eval_proof_fields(
        task, record_id, source_sha, eval_sha
    )
    row["generation"].update(
        eval_patch_sha256=eval_sha,
        filtered_patch_paths=paths,
    )
    summary = row["eval"].get("summary")
    if isinstance(summary, dict):
        summary.update(
            eval_patch_sha256=eval_sha,
            filtered_patch_paths=paths,
            candidate_expectation=expectation,
            candidate_projection=projection,
            source_candidate_projection=candidate_source_projection_fields(expectation),
        )
    return row


def _write(path: Path, row: dict, mtime: int) -> Path:
    path.write_text(json.dumps({"rows": [row]}), encoding="utf-8")
    os.utime(path, ns=(mtime, mtime))
    return path


def test_reconciliation_keeps_only_the_selected_eval_projection(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    current = _bound_row(82, task, "eval_done", True, "c" * 64, ["new.py"])
    same_projection = _bound_row(
        82, task, "technical_eval_failed", False, "c" * 64, ["new.py"]
    )
    different_paths = _bound_row(
        82, task, "technical_eval_failed", False, "c" * 64, ["old.py"]
    )
    different_hash = _bound_row(
        82, task, "technical_eval_failed", False, "a" * 64, ["old.py"]
    )
    current_path = _write(parent / "task_82_eval_only_current.json", current, 2)
    same_path = _write(parent / "task_82_eval_only_history.json", same_projection, 1)
    _write(parent / "task_82_eval_only_old_paths.json", different_paths, 3)
    _write(parent / "task_82_eval_only_old_hash.json", different_hash, 4)
    source_sha = current["generation"]["patch_sha256"]

    selected = eval_only_reconciliation_reports(
        parent,
        current_path,
        candidate_identities={82: (task, current["generation"]["record_id"], source_sha, "b" * 64)},
    )

    assert set(selected) == {current_path, same_path}
    report = build_report(selected, max_rounds=10, allow_over_budget_evidence=True)
    assert report["counts"]["eval_success"] == 1
    assert report["counts"]["resolved"] == 1
    assert report["counts"]["technical_failed_final"] == 0


def test_reconciliation_retains_an_unexecuted_second_index(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task82 = "instance_owner__repo-82"
    task83 = "instance_owner__repo-83"
    row82 = _bound_row(82, task82, "eval_done", True, "c" * 64, [])
    row83 = _bound_row(83, task83, "eval_done", False, "d" * 64, [])
    row83["eval"] = {"status": "would_eval", "executed": False}
    current = _write(parent / "task_82_eval_only_current.json", row82, 2)
    pending = _write(parent / "task_83_eval_only_pending.json", row83, 1)
    selected = eval_only_reconciliation_reports(
        parent,
        current,
        candidate_identities={
            82: (task82, row82["generation"]["record_id"], row82["generation"]["patch_sha256"], "b" * 64),
            83: (task83, row83["generation"]["record_id"], row83["generation"]["patch_sha256"], "d" * 64),
        },
    )

    assert set(selected) == {current, pending}


def test_bound_parent_filter_rejects_malformed_same_index_row():
    from opencollab_eval.commands.swe_v1_prolite_report import candidate_row_is_admitted

    expected = ("instance_owner__repo-82", "record", "a" * 64, "b" * 64)
    malformed = {
        "index": 82,
        "task": expected[0],
        "generation": {
            "source_patch_sha256": expected[2],
        },
    }
    assert candidate_row_is_admitted(malformed, expected) is False


def test_bound_parent_census_ignores_stale_row_index_from_old_candidate(tmp_path):
    """A stale nested result must not poison a later bound candidate verdict."""
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    current = _bound_row(82, task, "eval_done", True, "c" * 64, ["new.py"])
    current_path = _write(parent / "task_82_eval_only_current.json", current, 2)
    stale = {
        "index": 81,
        "task": "instance_owner__old-repo",
        "generation": {"record_id": "old-record", "patch_sha256": "d" * 64},
        "eval": {"status": "eval_done", "summary": {"resolved": False}},
    }
    parent_path = parent / "parallel_summary.json"
    parent_path.write_text(
        json.dumps(
            {
                "indices": [82],
                "results": [
                    {
                        "index": 82,
                        "completed": True,
                        "runner_status": "done",
                        "rows": [stale],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_sha = current["generation"]["patch_sha256"]
    identity = (task, current["generation"]["record_id"], source_sha, "b" * 64)

    report = build_report(
        [parent_path, current_path],
        max_rounds=10,
        allow_over_budget_evidence=True,
        candidate_identities={82: identity},
    )

    assert report["counts"]["eval_success"] == 1
    assert report["counts"]["resolved"] == 1
    assert report["counts"]["technical_failed_final"] == 0


def test_bound_parent_census_keeps_admitted_result_integrity_errors(tmp_path):
    """Binding a candidate must not hide errors on its admitted result wrapper."""
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    current = _bound_row(82, task, "eval_done", True, "c" * 64, ["new.py"])
    current_path = _write(parent / "task_82_eval_only_current.json", current, 2)
    current_row = dict(current)
    parent_path = parent / "parallel_summary.json"
    parent_path.write_text(
        json.dumps(
            {
                "indices": [82],
                "results": [
                    {
                        "index": 82,
                        "completed": False,
                        "runner_status": "done",
                        "rows": [current_row],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_sha = current["generation"]["patch_sha256"]
    identity = (task, current["generation"]["record_id"], source_sha, "b" * 64)

    report = build_report(
        [parent_path, current_path],
        max_rounds=10,
        allow_over_budget_evidence=True,
        candidate_identities={82: identity},
    )

    assert report["counts"]["technical_failed_final"] == 1
    assert "incomplete_orchestrator_result" in report["tasks"][0]["technical_reasons"]


def test_bound_parent_filter_accepts_source_only_legacy_identity():
    from opencollab_eval.commands.swe_v1_prolite_report import candidate_row_is_admitted

    task = "instance_owner__repo-82"
    source = "a" * 64
    row = {
        "index": 82,
        "task": task,
        "generation": {
            "record_id": "record",
            "patch_sha256": source,
        },
    }
    assert candidate_row_is_admitted(row, (task, "record", source, source)) is True


def test_candidate_identity_file_is_fail_closed_when_missing(tmp_path):
    from opencollab_eval.commands.swe_v1_prolite_report import load_candidate_identities_json

    with pytest.raises(ValueError, match="unavailable"):
        load_candidate_identities_json(str(tmp_path / "missing.json"))
