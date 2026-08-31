from __future__ import annotations

# ruff: noqa: F401, F403, F405, I001

import hashlib
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_v1_prolite_runner_test_support import *
from test_swe_eval_layer_report import _row


def test_eval_attempt_count_helpers_reject_lossy_integer_coercion():
    from opencollab_eval.commands import _swe_eval_layer_integrity as integrity
    from opencollab_eval.commands import swe_v1_prolite_controller as controller

    for value in (True, False, 1.9, "1.9", "not-a-count", -1):
        row = {"eval": {"attempt_count": value}}
        assert integrity.eval_attempt_count(row) == 0
        assert controller._row_eval_attempt_count(row) == 0

    row = {"eval": {"attempt_count": "2"}}
    assert integrity.eval_attempt_count(row) == 2
    assert controller._row_eval_attempt_count(row) == 2


def test_eval_only_reconciles_the_parent_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    parent_row = _row(
        82,
        task,
        "/run/task-82.log",
        10,
        "technical_eval_failed",
    )
    child_row = _row(82, task, "/run/task-82.log", 10, "eval_done", False)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "rows": [
                            parent_row
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    child = tmp_path / "task_82_report.json"
    child.write_text(
        json.dumps(
            {
                "rows": [
                    child_row
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(parent_output_dir=parent, json_output=child, usd_cny=None)

    result = runner.update_parent_fact_report(args)

    assert result["counts"]["unresolved"] == 1
    final = json.loads((parent / "final_eval_layer_report.json").read_text(encoding="utf-8"))
    assert final["counts"]["technical_failed_final"] == 0
    assert final["tasks"][0]["resolved"] is False


def test_eval_only_reconciliation_preserves_prior_task_results(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task_82 = "instance_owner__repo-82"
    task_83 = "instance_owner__repo-83"
    parent_rows = [
        _row(82, task_82, "/run/task-82.log", 10, "technical_eval_failed"),
        _row(83, task_83, "/run/task-83.log", 10, "technical_eval_failed"),
    ]
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": parent_rows}]}),
        encoding="utf-8",
    )
    prior = parent / "task_82_eval_only_old.json"
    prior.write_text(
        json.dumps({"rows": [_row(82, task_82, "/run/task-82.log", 10, "eval_done", True)]}),
        encoding="utf-8",
    )
    current = parent / "task_83_eval_only_current.json"
    current.write_text(
        json.dumps({"rows": [_row(83, task_83, "/run/task-83.log", 10, "eval_done", False)]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(parent_output_dir=parent, json_output=current, usd_cny=None)

    runner.update_parent_fact_report(args)

    final = json.loads((parent / "final_eval_layer_report.json").read_text(encoding="utf-8"))
    by_index = {task["index"]: task for task in final["tasks"]}
    assert by_index[82]["resolved"] is True
    assert by_index[83]["resolved"] is False


def test_eval_only_reconciliation_rejects_symlink_current_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    real = tmp_path / "real.json"
    real.write_text(
        json.dumps(
            {"rows": [_row(82, "instance_owner__repo-82", "/run/task.log", 10, "eval_done", True)]}
        ),
        encoding="utf-8",
    )
    current = parent / "current.json"
    current.symlink_to(real)

    with pytest.raises(RuntimeError, match="must be a regular file"):
        runner.eval_only_reconciliation_reports(parent, current)


def test_eval_only_reconciliation_keeps_execution_and_derived_verdict(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    executed = parent / "task_82_eval_only_executed.json"
    executed.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        **_row(82, task, "/run/task.log", 10, "technical_eval_failed"),
                        "eval": {
                            "status": "technical_eval_failed",
                            "attempt_count": 2,
                            "executed": True,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    derived = parent / "task_82_eval_only_rejudged.json"
    derived.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        **_row(82, task, "/run/task.log", 10, "eval_done", True),
                        "eval": {
                            "status": "eval_done",
                            "attempt_count": 2,
                            "executed": False,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    selected = runner.eval_only_reconciliation_reports(parent, derived)

    assert set(selected) == {executed, derived}


def test_eval_only_reconciliation_retains_prior_final_report_sources(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    prior = tmp_path / "task_82_eval_only_prior.json"
    prior.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        **_row(82, task, "/run/prior.log", 10, "technical_eval_failed"),
                        "eval": {
                            "status": "technical_eval_failed",
                            "attempt_count": 2,
                            "executed": True,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps(
            {
                "source_reports": [
                    str(parent / "parallel_summary.json"),
                    str(prior),
                ]
            }
        ),
        encoding="utf-8",
    )
    current = tmp_path / "task_82_eval_only_current.json"
    current.write_text(
        json.dumps({"rows": [_row(82, task, "/run/current.log", 10, "eval_done", True)]}),
        encoding="utf-8",
    )

    selected = runner.eval_only_reconciliation_reports(parent, current)

    assert set(selected) == {prior, current}


def test_eval_only_reconciliation_rejects_a_corrupt_prior_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "final_eval_layer_report.json").write_text("{", encoding="utf-8")
    current = tmp_path / "task_82_eval_only_current.json"
    current.write_text(
        json.dumps(
            {
                "rows": [
                    _row(
                        82,
                        "instance_owner__repo-82",
                        "/run/current.log",
                        10,
                        "eval_done",
                        True,
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid_report_json"):
        runner.eval_only_reconciliation_reports(parent, current)


@pytest.mark.parametrize("source_reports", [None, {}, [], ["relative.json"], [1]])
def test_eval_only_reconciliation_rejects_invalid_historical_sources(
    tmp_path,
    source_reports,
):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps({"source_reports": source_reports}),
        encoding="utf-8",
    )
    current = tmp_path / "task_82_eval_only_current.json"
    current.write_text(
        json.dumps(
            {
                "rows": [
                    _row(
                        82,
                        "instance_owner__repo-82",
                        "/run/current.log",
                        10,
                        "eval_done",
                        True,
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid source_reports"):
        runner.eval_only_reconciliation_reports(parent, current)


def test_eval_only_reconciliation_uses_recency_for_a_derived_verdict(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    older = parent / "task_82_eval_only_old.json"
    older.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        **_row(82, task, "/run/old.log", 10, "eval_done", False),
                        "eval": {
                            "status": "eval_done",
                            "attempt_count": 2,
                            "executed": False,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    newer = parent / "task_82_eval_only_new.json"
    newer.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        **_row(82, task, "/run/new.log", 10, "eval_done", True),
                        "eval": {
                            "status": "eval_done",
                            "attempt_count": 1,
                            "executed": False,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    selected = runner.eval_only_reconciliation_reports(parent, newer)

    assert selected == [newer]


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


def test_eval_only_parent_budget_keeps_the_current_run_limit_independent(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 1},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=1,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["effective_additional_eval_attempts"] == 1
    assert budget["effective_max_eval_attempts"] == 1
    assert budget["projected_total_eval_attempts"] == 2
    assert budget["max_total_eval_attempts"] == 10
    assert args.max_eval_attempts == 1


def test_eval_only_parent_budget_allows_recovery_after_two_prior_attempts(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 2},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["previous_eval_attempts"][82] == 2
    assert budget["effective_additional_eval_attempts"] == 2
    assert budget["projected_total_eval_attempts"] == 4
    assert args.max_eval_attempts == 2


def test_eval_only_parent_budget_uses_the_updated_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 1},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps({"tasks": [{"index": 82, "observed_eval_attempt_count": 9}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["previous_eval_attempts"][82] == 9
    assert budget["effective_additional_eval_attempts"] == 1
    assert budget["projected_total_eval_attempts"] == 10
    assert args.max_eval_attempts == 1


def test_eval_only_parent_budget_adds_split_parent_attempts(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    rows = [
        {
            "index": 82,
            "task": "instance_owner__repo-82",
            "eval": {"status": "technical_eval_failed", "attempt_count": 1},
        },
        {
            "index": 82,
            "task": "instance_owner__repo-82",
            "eval": {"status": "technical_eval_failed", "attempt_count": 1},
        },
    ]
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [rows[0]]}, {"rows": [rows[1]]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["previous_eval_attempts"][82] == 2
    assert budget["effective_additional_eval_attempts"] == 2
    assert budget["projected_total_eval_attempts"] == 4
    assert args.max_eval_attempts == 2


def test_eval_only_parent_budget_rejects_after_ten_attempts(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 10},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=1,
    )

    with pytest.raises(RuntimeError, match="max total is 10"):
        runner.apply_parent_eval_budget(args)


def test_eval_only_parent_budget_does_not_count_a_dry_run(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "would_eval", "attempt_count": 0},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["effective_max_eval_attempts"] == 2
    assert budget["projected_total_eval_attempts"] == 2


def test_parent_eval_lock_excludes_a_second_process(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    lock_path = parent / ".eval_only.lock"
    with runner.ParentEvalLock(parent):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,sys; "
                    "handle=open(sys.argv[1], 'a+'); "
                    "\ntry:\n"
                    " fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n"
                    " raise SystemExit(0)\n"
                    "raise SystemExit(1)"
                ),
                str(lock_path),
            ],
            text=True,
            capture_output=True,
        )
    assert probe.returncode == 0, probe.stderr


def test_eval_only_task_locks_allow_distinct_indices_to_run_independently(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    first = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=25,
        limit=1,
    )
    second = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=27,
        limit=1,
    )

    with runner.parent_eval_lock(first) as first_lock:
        with runner.parent_eval_lock(second) as second_lock:
            assert first_lock.path != second_lock.path
            assert first_lock.path.name == ".eval_only.task-25-25.lock"
            assert second_lock.path.name == ".eval_only.task-27-27.lock"


def test_eval_only_cli_requires_a_parent_output_dir(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "opencollab_eval.commands.swe_v1_prolite_runner", "--eval-only", "--dry-run"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "--eval-only requires --parent-output-dir" in proc.stderr


def test_eval_only_cli_rejects_a_multi_task_slice(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "opencollab_eval.commands.swe_v1_prolite_runner",
            "--eval-only",
            "--parent-output-dir",
            str(tmp_path),
            "--limit",
            "2",
            "--dry-run",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "--eval-only requires --limit 1" in proc.stderr


def test_eval_only_cli_requires_zero_task_starts(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "opencollab_eval.commands.swe_v1_prolite_runner",
            "--eval-only",
            "--parent-output-dir",
            str(tmp_path),
            "--limit",
            "1",
            "--max-task-starts",
            "1",
            "--dry-run",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "--eval-only requires --max-task-starts 0" in proc.stderr
