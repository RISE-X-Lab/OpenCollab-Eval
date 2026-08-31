from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import swe_rejudge_queue as queue


def _accept_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        queue._swe_eval_layer_integrity,
        "attempt_integrity",
        lambda row, task: SimpleNamespace(
            direct_execution_proven=True,
            reasons=(),
        ),
    )


def _terminal_report(
    path: Path,
    *,
    index: int,
    patch_sha256: str,
    resolved: bool,
    summary_status: str = "eval_done",
) -> None:
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": index,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": patch_sha256,
                            "source_patch_sha256": patch_sha256,
                            "eval_patch_sha256": patch_sha256,
                        },
                        "eval": {
                            "status": "eval_done",
                            "summary": {"status": summary_status, "resolved": resolved},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _plan(tmp_path: Path, *, patch_sha256: str = "a" * 64) -> tuple[Path, Path]:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "parallel_summary.json").write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema": queue.SCHEMA,
                "runner_args": ["--host", "worker"],
                "jobs": [
                    {
                        "index": 25,
                        "parent_output_dir": str(parent),
                        "base_run_dir": "/worker/run/task_25",
                        "remote_runtime_repo": "/worker/runtime/task_25",
                        "run_id": "rejudge-task-25",
                        "eval_dir_name": "official_eval_rejudge",
                        "task": "instance_owner__repo-25",
                        "record_id": "record-25",
                        "source_patch_sha256": patch_sha256,
                        "eval_patch_sha256": patch_sha256,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return plan, parent


def test_queue_child_timeout_kills_descendants_in_owned_process_group(tmp_path):
    if not hasattr(queue.os, "killpg"):
        pytest.skip("queue child cleanup uses POSIX process groups")
    pid_file = tmp_path / "descendant.pid"
    command = (
        "import pathlib, subprocess, sys, time;"
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
        "time.sleep(30)"
    )
    log_path = tmp_path / "queue.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = queue._run_bounded_child(
            [sys.executable, "-c", command, str(pid_file)],
            log=log,
            timeout=0.2,
        )

    assert result.returncode == 124
    assert "queue child timeout" in log_path.read_text(encoding="utf-8")
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            queue.os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("timed-out queue child left its descendant running")


def test_queue_timeout_resolution_honors_job_runner_and_environment(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_EVAL_TIMEOUT_SECONDS", "17")
    job = {"eval_timeout": 11}
    plan = {"runner_args": ["--eval-timeout", "13"]}

    assert queue._job_eval_timeout(plan, job) == 11
    assert queue._job_eval_timeout(plan, {}) == 13
    assert queue._job_eval_timeout({"runner_args": []}, {}) == 17
    assert queue._job_eval_timeout({"runner_args": []}, {"eval_timeout": 19}) == 19


def test_queue_invalid_environment_timeout_is_task_scoped(tmp_path, monkeypatch):
    """A malformed timeout must not leave a persisted running checkpoint."""
    plan_path = tmp_path / "plan.json"
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "parallel_summary.json").write_text(
        '{"rows":[{"index":1,"task":"t","generation":{"record_id":"r",'
        '"patch_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"source_patch_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"eval_patch_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]}',
        encoding="utf-8",
    )
    plan_path.write_text(
        json.dumps(
            {
                "schema": queue.SCHEMA,
                "runner_args": [],
                "jobs": [
                    {
                        "index": 1,
                        "parent_output_dir": str(parent),
                        "base_run_dir": "/worker/run",
                        "remote_runtime_repo": "/worker/runtime",
                        "run_id": "run-1",
                        "eval_dir_name": "eval",
                        "task": "t",
                        "record_id": "r",
                        "source_patch_sha256": "a" * 64,
                        "eval_patch_sha256": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_EVAL_TIMEOUT_SECONDS", "nan")
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})

    result = queue.run_queue(plan_path, tmp_path / "state", workers=1)

    assert result["counts"] == {"invalid_timeout": 1}
    assert next(iter(result["jobs"].values()))["status"] == "invalid_timeout"


def test_queue_ignores_partial_historical_artifact_before_startup(tmp_path, monkeypatch):
    """A stale partial JSON must not abort queue startup or parent reconciliation."""
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (parent / "task_25_eval_only_partial.json").write_text(
        '{"rows":[{"index":25,"eval":"still-writing"}]}', encoding="utf-8"
    )
    monkeypatch.setattr(
        queue,
        "_run_bounded_child",
        lambda argv, *, log, timeout: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: (_ for _ in ()).throw(RuntimeError("partial artifact")),
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"command_failed": 1}


def test_queue_prefers_the_planned_identity_over_historical_rows(
    tmp_path, monkeypatch
):
    """A stale report must not block a candidate that matches the queue plan."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "task_25_eval_only_historical.json",
        index=25,
        patch_sha256="b" * 64,
        resolved=False,
    )
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def fake_run(argv, *, log, timeout):
        nonlocal calls
        del log, timeout
        calls += 1
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    refresh_calls = []
    monkeypatch.setattr(
        queue, "update_parent_fact_report", lambda args: refresh_calls.append(args)
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert calls == 1
    assert result["counts"] == {"terminal": 1}


def test_queue_does_not_promote_a_cleanup_failure_to_terminal(
    tmp_path, monkeypatch
):
    """A valid report cannot hide a non-zero child/cleanup result."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    calls = 0

    def fake_run(argv, *, log, timeout):
        nonlocal calls
        del log, timeout
        calls += 1
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=125)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    refresh_calls = []
    monkeypatch.setattr(
        queue, "update_parent_fact_report", lambda args: refresh_calls.append(args)
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"command_failed": 1}
    assert calls == 1
    assert refresh_calls == []
    assert not list(parent.glob("task_*_eval_only_*.json"))
    assert next(iter(result["jobs"].values()))["status"] == "command_failed"


def test_queue_malformed_child_report_is_task_scoped_and_bounded(
    tmp_path, monkeypatch
):
    """A malformed report cannot crash the queue or erase its launch count."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def fake_run(argv, *, log, timeout):
        nonlocal calls
        del log, timeout
        calls += 1
        output = Path(argv[argv.index("--json-output") + 1])
        output.write_text(json.dumps({"rows": [{"eval": "invalid"}]}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"command_failed": 1}
    assert calls == 2
    job_state = next(iter(result["jobs"].values()))
    assert job_state["launch_count"] == 2


def test_queue_ignores_final_attempt_count_from_another_candidate(tmp_path):
    plan, parent = _plan(tmp_path)
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-old",
                        "record_id": "record-old",
                        "patch_sha256": "b" * 64,
                        "observed_eval_attempt_count": 10,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    job = queue._read_plan(plan)["jobs"][0]

    assert queue._observed_eval_attempts(job) == 0


def test_queue_uses_final_attempt_count_only_for_exact_candidate(tmp_path):
    plan, parent = _plan(tmp_path)
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "record_id": "record-25",
                        "source_patch_sha256": "a" * 64,
                        "eval_patch_sha256": "a" * 64,
                        "observed_eval_attempt_count": 7,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    job = queue._read_plan(plan)["jobs"][0]

    assert queue._observed_eval_attempts(job) == 7


def test_queue_ignores_parent_attempt_count_from_another_candidate(tmp_path):
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "rows": [
                            {
                                "index": 25,
                                "task": "instance_owner__repo-old",
                                "generation": {
                                    "record_id": "record-old",
                                    "patch_sha256": "b" * 64,
                                    "source_patch_sha256": "b" * 64,
                                    "eval_patch_sha256": "b" * 64,
                                },
                                "eval": {"attempt_count": 10},
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    job = queue._read_plan(plan)["jobs"][0]

    assert queue._observed_eval_attempts(job) == 0


def test_queue_retries_a_transient_child_spawn_exception(
    tmp_path,
    monkeypatch,
):
    """A child launch error is task-scoped and consumes one bounded retry."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def fake_run(argv, *, log, timeout):
        nonlocal calls
        del log, timeout
        calls += 1
        if calls == 1:
            raise OSError("transient evaluator spawn failure")
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=False)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert calls == 2
    assert result["counts"] == {"terminal": 1}
    job_state = next(iter(result["jobs"].values()))
    assert job_state["launch_count"] == 2
    assert "transient evaluator spawn failure" not in job_state


def test_queue_treats_corrupt_persisted_launch_count_as_exhausted(
    tmp_path,
    monkeypatch,
):
    """A malformed checkpoint must not be coerced to a fresh retry budget."""
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    normalized = queue._read_plan(plan)
    queue_id = queue.hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    output_dir = tmp_path / "state"
    output_dir.mkdir()
    key = queue._job_key(normalized["jobs"][0])
    (output_dir / f"rejudge_queue_{queue_id}.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.eval_only_queue_state.v1",
                "queue_id": queue_id,
                "jobs": {key: {"status": "running", "launch_count": "oops"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue,
        "_run_bounded_child",
        lambda *args, **kwargs: pytest.fail("corrupt state must not launch a child"),
    )

    result = queue.run_queue(plan, output_dir, workers=1)

    assert result["counts"] == {"launch_budget_exhausted": 1}
    assert next(iter(result["jobs"].values()))["launch_count"] == 2


def test_queue_refreshes_parent_for_later_job_after_summary_terminal(
    tmp_path, monkeypatch
):
    """A terminal summary row must not hide a later task report."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    value = json.loads(plan.read_text(encoding="utf-8"))
    first = value["jobs"][0]
    second = {
        **first,
        "index": 27,
        "base_run_dir": "/worker/run/task_27",
        "remote_runtime_repo": "/worker/runtime/task_27",
        "run_id": "rejudge-task-27",
        "task": "instance_owner__repo-27",
        "record_id": "record-27",
        "source_patch_sha256": "b" * 64,
        "eval_patch_sha256": "b" * 64,
    }
    value["jobs"].append(second)
    plan.write_text(json.dumps(value), encoding="utf-8")
    rows = []
    for job in value["jobs"]:
        row = {
            "index": job["index"],
            "task": job["task"],
            "generation": {
                "record_id": job["record_id"],
                "patch_sha256": job["source_patch_sha256"],
                "source_patch_sha256": job["source_patch_sha256"],
                "eval_patch_sha256": job["eval_patch_sha256"],
            },
        }
        if job is first:
            row["eval"] = {
                "status": "eval_done",
                "summary": {"resolved": False},
            }
        rows.append(row)
    (parent / "parallel_summary.json").write_text(
        json.dumps({"rows": rows}), encoding="utf-8"
    )
    child_reports = []

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=27, patch_sha256="b" * 64, resolved=True)
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["rows"][0]["task"] = second["task"]
        payload["rows"][0]["generation"]["record_id"] = second["record_id"]
        output.write_text(json.dumps(payload), encoding="utf-8")
        child_reports.append(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    refreshes = []
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: refreshes.append(args) or {"status": "done"},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"skipped_terminal": 1, "terminal": 1}
    assert len(refreshes) == 1
    assert child_reports == [refreshes[0].json_output]


def test_queue_quarantines_malformed_historical_report_before_refresh(
    tmp_path, monkeypatch
):
    """One bad historical file must not erase a valid new terminal result."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    malformed = parent / "task_25_eval_only_partial.json"
    malformed.write_text('{"rows":[', encoding="utf-8")

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    refreshes = []
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: refreshes.append(args) or {"status": "done"},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"terminal": 1}
    assert len(refreshes) == 1
    assert not malformed.exists()
    backups = list(parent.glob("task_25_eval_only_partial.json.invalid*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"rows":['


def test_queue_quarantine_does_not_overwrite_existing_backup(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": 25,
                        "task": "instance_owner__repo-25",
                        "generation": {
                            "record_id": "record-25",
                            "patch_sha256": "a" * 64,
                            "source_patch_sha256": "a" * 64,
                            "eval_patch_sha256": "a" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    malformed = parent / "task_25_eval_only_partial.json"
    malformed.write_text("bad", encoding="utf-8")
    existing = parent / "task_25_eval_only_partial.json.invalid"
    existing.write_text("keep", encoding="utf-8")

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"terminal": 1}
    assert existing.read_text(encoding="utf-8") == "keep"
    assert (parent / "task_25_eval_only_partial.json.invalid.1").read_text(
        encoding="utf-8"
    ) == "bad"
