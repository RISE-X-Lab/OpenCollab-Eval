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
