from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
    trusted_summary_proof_fields,
)
from swe_v1_prolite_runner_test_support import runner
from test_swe_eval_layer_report import _row


def test_eval_only_reconciliation_scopes_late_verdict_to_planned_identity(tmp_path):
    """A late report from another candidate must not replace the current one."""
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    source_sha = "a" * 64
    eval_sha = "b" * 64

    def write_report(
        name: str,
        *,
        record_id: str,
        source: str,
        evaluated: str,
        resolved: bool,
        attempts: int,
    ) -> Path:
        path = parent / name
        path.write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "index": 82,
                            "task": task,
                            "generation": {
                                "task": task,
                                "record_id": record_id,
                                "source_patch_sha256": source,
                                "eval_patch_sha256": evaluated,
                            },
                            "eval": {
                                "task": task,
                                "status": "eval_done",
                                "attempt_count": attempts,
                                "summary": {
                                    "task": task,
                                    "record_id": record_id,
                                    "patch_sha256": source,
                                    "eval_patch_sha256": evaluated,
                                    "resolved": resolved,
                                },
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    historical_same = write_report(
        "task_82_eval_only_same_candidate.json",
        record_id="record-current",
        source=source_sha,
        evaluated=eval_sha,
        resolved=False,
        attempts=2,
    )
    current = write_report(
        "task_82_eval_only_current.json",
        record_id="record-current",
        source=source_sha,
        evaluated=eval_sha,
        resolved=True,
        attempts=1,
    )
    late_other = write_report(
        "task_82_eval_only_late_other.json",
        record_id="record-old",
        source="c" * 64,
        evaluated="d" * 64,
        resolved=False,
        attempts=1,
    )
    os.utime(historical_same, ns=(1, 1))
    os.utime(current, ns=(2, 2))
    # Simulate a previous evaluator that finished after the current attempt.
    os.utime(late_other, ns=(3, 3))

    selected = runner.eval_only_reconciliation_reports(
        parent,
        current,
        candidate_identities={
            82: (task, "record-current", source_sha, eval_sha),
        },
    )

    assert set(selected) == {historical_same, current}
    assert late_other not in selected


def test_eval_only_reconciliation_accepts_legacy_task_id_alias(tmp_path):
    """A legacy ``task_id`` row remains reusable for its bound candidate."""
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    source_sha = "a" * 64
    report = parent / "task_82_eval_only_legacy.json"
    report.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 82,
                        "task_id": task,
                        "generation": {
                            "record_id": "record-current",
                            "source_patch_sha256": source_sha,
                            "eval_patch_sha256": source_sha,
                        },
                        "eval": {
                            "status": "eval_done",
                            "summary": {"resolved": True},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    selected = runner.eval_only_reconciliation_reports(
        parent,
        report,
        candidate_identities={
            82: (task, "record-current", source_sha, source_sha),
        },
    )

    assert selected == [report]

def test_eval_only_reconciliation_accepts_recomputed_eval_hash_and_execution_history(
    tmp_path,
):
    """Derived eval hashes and summary omissions must not hide one candidate."""
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    source_sha = "a" * 64
    expected_eval_sha = "b" * 64
    recomputed_eval_sha = "c" * 64

    execution_only = parent / "task_82_eval_only_execution.json"
    execution_only.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 82,
                        "task": task,
                        "generation": {
                            "record_id": "record-current",
                            "patch_sha256": source_sha,
                            "source_patch_sha256": source_sha,
                            "eval_patch_sha256": recomputed_eval_sha,
                        },
                        # Historical execution evidence can have no summary.
                        "eval": {
                            "status": "technical_eval_failed",
                            "executed": True,
                            "attempt_count": 2,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    current = parent / "task_82_eval_only_current_recomputed.json"
    current.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 82,
                        "task": task,
                        "generation": {
                            "record_id": "record-current",
                            "patch_sha256": source_sha,
                            "source_patch_sha256": source_sha,
                            "eval_patch_sha256": recomputed_eval_sha,
                        },
                        "eval": {
                            "status": "eval_done",
                            "executed": False,
                            "summary": {"resolved": True},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    late_other = parent / "task_82_eval_only_late_other_recomputed.json"
    late_other.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 82,
                        "task": task,
                        "generation": {
                            "record_id": "record-old",
                            "patch_sha256": "d" * 64,
                            "source_patch_sha256": "d" * 64,
                            "eval_patch_sha256": "e" * 64,
                        },
                        "eval": {
                            "status": "eval_done",
                            "executed": False,
                            "summary": {"resolved": False},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    os.utime(execution_only, ns=(1, 1))
    os.utime(current, ns=(2, 2))
    os.utime(late_other, ns=(3, 3))

    selected = runner.eval_only_reconciliation_reports(
        parent,
        current,
        candidate_identities={
            82: (task, "record-current", source_sha, expected_eval_sha),
        },
    )

    assert set(selected) == {execution_only, current}
    assert late_other not in selected

def test_update_parent_report_binds_legacy_candidate_without_eval_hash(
    tmp_path, monkeypatch
):
    """Task/record/source still scope legacy callers that omit eval SHA."""
    from opencollab_eval.commands import swe_v1_prolite_controller as controller

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "parallel_summary.json").write_text("{}", encoding="utf-8")
    current = parent / "task_82_eval_only_current.json"
    current.write_text("{\"rows\": []}", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_reconcile(parent_dir, report_path, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(controller, "eval_only_reconciliation_reports", fake_reconcile)

    def fake_run(command, *, text, capture_output, cwd):
        del command, text, capture_output, cwd
        (parent / "final_eval_layer_report.json").write_text(
            json.dumps({"counts": {}}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        json_output=current,
        usd_cny=None,
        start_index=82,
        expected_task="instance_owner__repo-82",
        expected_record_id="record-current",
        expected_source_patch_sha256="a" * 64,
        expected_eval_patch_sha256="",
    )

    controller.update_parent_fact_report(args)

    assert captured["candidate_identities"] == {
        82: (
            "instance_owner__repo-82",
            "record-current",
            "a" * 64,
            "a" * 64,
        )
    }


def test_update_parent_report_filters_old_same_index_candidate(tmp_path):
    """A queue-bound rejudge must replace, not merge, an older parent candidate."""
    from opencollab_eval.commands import swe_v1_prolite_controller as controller

    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"

    def make_row(record_id: str, source: str, evaluated: str, resolved: bool) -> dict:
        value = _row(82, task, f"/run/{record_id}.log", 10, "eval_done", resolved)
        value["generation"].update(
            record_id=record_id,
            patch_sha256=source,
            source_patch_sha256=source,
            eval_patch_sha256=evaluated,
            **trusted_summary_proof_fields(source),
        )
        expectation, projection = candidate_eval_proof_fields(
            task, record_id, source, evaluated
        )
        value["eval"]["summary"].update(
            record_id=record_id,
            patch_sha256=source,
            eval_patch_sha256=evaluated,
            candidate_expectation=expectation,
            candidate_projection=projection,
            source_candidate_projection=candidate_source_projection_fields(expectation),
        )
        return value

    old = make_row("record-old", "a" * 64, "a" * 64, False)
    current = make_row("record-current", "b" * 64, "c" * 64, True)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_parallel_runner.v2",
                "indices": [82],
                "results": [
                    {
                        "index": 82,
                        "completed": True,
                        "runner_status": "done",
                        "rows": [old],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    child = parent / "task_82_eval_only_current.json"
    child.write_text(json.dumps({"rows": [current]}), encoding="utf-8")

    controller.update_parent_fact_report(
        SimpleNamespace(
            parent_output_dir=parent,
            json_output=child,
            usd_cny=None,
            candidate_identities={82: (task, "record-current", "b" * 64, "c" * 64)},
        )
    )

    final = json.loads((parent / "final_eval_layer_report.json").read_text())
    assert final["counts"]["eval_success"] == 1
    assert final["counts"]["resolved"] == 1
    assert final["counts"]["technical_failed_final"] == 0
    assert not list(parent.glob(".candidate-identities-*.json"))


def test_eval_attempt_budget_deduplicates_top_level_legacy_mirror(tmp_path):
    """A legacy top-level/results mirror must consume one cumulative count."""
    from opencollab_eval.commands import swe_v1_prolite_controller as controller

    parent = tmp_path / "parent"
    parent.mkdir()
    patch = "a" * 64
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "generation": {
            "record_id": "record-82",
            "patch_sha256": patch,
            "source_patch_sha256": patch,
            "eval_patch_sha256": patch,
        },
        "eval": {"status": "technical_eval_failed", "attempt_count": 3},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"rows": [row], "results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
        expected_task=row["task"],
        expected_record_id="record-82",
        expected_source_patch_sha256=patch,
        expected_eval_patch_sha256=patch,
    )

    budget = controller.apply_parent_eval_budget(args)

    assert budget["previous_eval_attempts"][82] == 3
    assert budget["remaining_by_index"][82] == 7


def test_eval_attempt_budget_preserves_nested_duplicate_ledgers(tmp_path):
    """Separate nested result ledgers remain separate cumulative histories."""
    from opencollab_eval.commands import swe_v1_prolite_controller as controller

    parent = tmp_path / "parent"
    parent.mkdir()
    patch = "a" * 64
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "generation": {
            "record_id": "record-82",
            "patch_sha256": patch,
            "source_patch_sha256": patch,
            "eval_patch_sha256": patch,
        },
        "eval": {"status": "technical_eval_failed", "attempt_count": 3},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}, {"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
        expected_task=row["task"],
        expected_record_id="record-82",
        expected_source_patch_sha256=patch,
        expected_eval_patch_sha256=patch,
    )

    budget = controller.apply_parent_eval_budget(args)

    assert budget["previous_eval_attempts"][82] == 6
