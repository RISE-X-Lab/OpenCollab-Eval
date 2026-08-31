from __future__ import annotations

import json
import subprocess
import sys
import threading
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


def test_queue_rejects_conflicting_terminal_verdicts(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "task_25_eval_only_seed.json",
        index=25,
        patch_sha256="a" * 64,
        resolved=False,
    )
    stale = parent / "task_25_eval_only_stale.json"
    _terminal_report(stale, index=25, patch_sha256="a" * 64, resolved=True)
    current = parent / "task_25_eval_only_current.json"
    _terminal_report(current, index=25, patch_sha256="a" * 64, resolved=False)
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )
    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"terminal_verdict_conflict": 1}
    assert result["model_generation"] == "disabled"
    assert next(iter(result["jobs"].values()))["status"] == "terminal_verdict_conflict"


def test_queue_skips_a_single_exact_terminal_candidate(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    current = parent / "task_25_eval_only_current.json"
    _terminal_report(current, index=25, patch_sha256="a" * 64, resolved=False)
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"skipped_terminal": 1}
    assert next(iter(result["jobs"].values()))["report"] == str(current)


def test_queue_accepts_a_direct_eval_done_summary(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    current = parent / "task_25_deadbeef_eval_only_queue_current.json"
    _terminal_report(
        current,
        index=25,
        patch_sha256="a" * 64,
        resolved=False,
        summary_status="done",
    )
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"skipped_terminal": 1}
    assert next(iter(result["jobs"].values()))["report"] == str(current)


def test_queue_does_not_accept_a_weak_terminal_report(tmp_path):
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "task_25_eval_only_weak.json",
        index=25,
        patch_sha256="a" * 64,
        resolved=True,
    )
    job = queue._read_plan(plan)["jobs"][0]

    assert queue._terminal_report(job) == (None, "missing")


def test_queue_recognizes_a_terminal_in_the_original_parent_report(
    tmp_path,
    monkeypatch,
):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "parallel_summary.json",
        index=25,
        patch_sha256="a" * 64,
        resolved=True,
    )
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"skipped_terminal": 1}
    assert next(iter(result["jobs"].values()))["report"] == str(
        parent / "parallel_summary.json"
    )


def test_queue_rejects_ambiguous_identities_without_the_planned_candidate(
    tmp_path, monkeypatch
):
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "task_25_eval_only_historical_a.json",
        index=25,
        patch_sha256="b" * 64,
        resolved=False,
    )
    _terminal_report(
        parent / "task_25_eval_only_historical_b.json",
        index=25,
        patch_sha256="c" * 64,
        resolved=True,
    )
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"candidate_identity_conflict": 1}


def test_queue_runs_eval_only_with_generation_disabled(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    _terminal_report(
        parent / "task_25_eval_only_seed.json",
        index=25,
        patch_sha256="a" * 64,
        resolved=False,
    )
    (parent / "task_25_eval_only_seed.json").unlink()
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
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
                ]
            }
        ),
        encoding="utf-8",
    )
    seen: list[list[str]] = []

    def fake_run(argv, *, log, timeout):
        del log, timeout
        seen.append(argv)
        json_output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(json_output, index=25, patch_sha256="a" * 64, resolved=True)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )
    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"terminal": 1}
    argv = seen[0]
    assert "--eval-only" in argv
    assert argv[argv.index("--max-task-starts") + 1] == "0"
    assert argv[argv.index("--max-empty-patch-retries") + 1] == "0"
    assert argv[argv.index("--limit") + 1] == "1"
    assert str(parent) == argv[argv.index("--parent-output-dir") + 1]
    assert argv[argv.index("--expected-task") + 1] == "instance_owner__repo-25"
    assert argv[argv.index("--expected-record-id") + 1] == "record-25"


def test_queue_skips_a_ten_attempt_candidate_without_starting_a_child(
    tmp_path,
    monkeypatch,
):
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
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
                                "eval": {
                                    "status": "technical_eval_failed",
                                    "attempt_count": 10,
                                },
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )
    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"budget_exhausted": 1}


def test_queue_allows_recovery_after_two_prior_attempts(tmp_path, monkeypatch):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
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
                                "eval": {
                                    "status": "technical_eval_failed",
                                    "attempt_count": 2,
                                },
                            }
                        ]
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
        json_output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(json_output, index=25, patch_sha256="a" * 64, resolved=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert calls == 1
    assert result["counts"] == {"terminal": 1}


@pytest.mark.parametrize(
    "runner_args",
    [
        ["--eval-only"],
        ["--start-index=25"],
        ["--max-task-starts", "1"],
    ],
)
def test_queue_rejects_runner_args_that_can_enable_or_redirect_generation(
    tmp_path,
    runner_args,
):
    plan, _parent = _plan(tmp_path)
    value = json.loads(plan.read_text(encoding="utf-8"))
    value["runner_args"] = runner_args
    plan.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="queue-owned option"):
        queue.run_queue(plan, tmp_path / "state", workers=2)


def test_queue_rejects_a_duplicate_candidate_across_parents(tmp_path):
    plan, parent = _plan(tmp_path)
    other_parent = tmp_path / "other-parent"
    other_parent.mkdir()
    value = json.loads(plan.read_text(encoding="utf-8"))
    duplicate = dict(value["jobs"][0])
    duplicate.update(
        parent_output_dir=str(other_parent),
        base_run_dir="/worker/run/duplicate",
        remote_runtime_repo="/worker/runtime/duplicate",
        run_id="duplicate",
    )
    value["jobs"].append(duplicate)
    plan.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate candidate identity"):
        queue._read_plan(plan)


def test_same_index_in_different_parents_gets_distinct_log_names(tmp_path):
    plan, _parent = _plan(tmp_path)
    value = queue._read_plan(plan)
    first = value["jobs"][0]
    other_parent = tmp_path / "other-parent"
    other_parent.mkdir()
    second = {
        **first,
        "parent_output_dir": str(other_parent),
        "task": "instance_owner__repo-other",
        "record_id": "record-other",
        "source_patch_sha256": "b" * 64,
        "eval_patch_sha256": "b" * 64,
    }

    first_argv = queue._child_argv(
        value, first, queue_id="queue", output_dir=tmp_path
    )
    second_argv = queue._child_argv(
        value, second, queue_id="queue", output_dir=tmp_path
    )

    assert first_argv[1].stem != second_argv[1].stem


def test_two_distinct_jobs_run_concurrently_and_persist_both_states(
    tmp_path,
    monkeypatch,
):
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    value = json.loads(plan.read_text(encoding="utf-8"))
    second = {
        **value["jobs"][0],
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
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "index": job["index"],
                        "task": job["task"],
                        "generation": {
                            "record_id": job["record_id"],
                            "patch_sha256": job["source_patch_sha256"],
                            "source_patch_sha256": job["source_patch_sha256"],
                            "eval_patch_sha256": job["eval_patch_sha256"],
                        },
                    }
                    for job in value["jobs"]
                ]
            }
        ),
        encoding="utf-8",
    )
    barrier = threading.Barrier(2)

    def fake_run(argv, *, log, timeout):
        del log, timeout
        barrier.wait(timeout=5)
        index = int(argv[argv.index("--start-index") + 1])
        digest = "a" * 64 if index == 25 else "b" * 64
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(output, index=index, patch_sha256=digest, resolved=False)
        if index == 27:
            report = json.loads(output.read_text(encoding="utf-8"))
            report["rows"][0]["task"] = "instance_owner__repo-27"
            report["rows"][0]["generation"]["record_id"] = "record-27"
            output.write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=2)

    assert result["counts"] == {"terminal": 2}
    state_path = next((tmp_path / "state").glob("rejudge_queue_*.json"))
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted["jobs"]) == 2
    assert {job["status"] for job in persisted["jobs"].values()} == {"terminal"}


def test_queue_retries_a_pre_eval_command_failure_without_model_generation(
    tmp_path,
    monkeypatch,
):
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
        if calls == 2:
            output = Path(argv[argv.index("--json-output") + 1])
            _terminal_report(output, index=25, patch_sha256="a" * 64, resolved=False)
        return SimpleNamespace(returncode=1 if calls == 1 else 0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: {"status": "done", "report_json": str(args.json_output)},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert calls == 2
    assert result["counts"] == {"terminal": 1}
    assert next(iter(result["jobs"].values()))["launch_count"] == 2


def test_persisted_launch_budget_prevents_unbounded_restarts(tmp_path, monkeypatch):
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
        del argv, log, timeout
        calls += 1
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    monkeypatch.setattr(queue, "update_parent_fact_report", lambda args: {})
    state_dir = tmp_path / "state"

    first = queue.run_queue(plan, state_dir, workers=1)
    second = queue.run_queue(plan, state_dir, workers=1)

    assert calls == 2
    assert first["counts"] == {"command_failed": 1}
    assert second["counts"] == {"launch_budget_exhausted": 1}


def test_second_queue_process_is_rejected_while_the_owner_lock_is_held(tmp_path):
    plan_path, _parent = _plan(tmp_path)
    output_dir = tmp_path / "state"
    output_dir.mkdir()
    plan = queue._read_plan(plan_path)
    queue_id = queue.hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    lock_path = output_dir / f".eval_only.rejudge-queue-{queue_id}.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,sys,time;"
                "handle=open(sys.argv[1],'a+');"
                "fcntl.flock(handle.fileno(),fcntl.LOCK_EX);"
                "print('ready',flush=True);"
                "time.sleep(30)"
            ),
            str(lock_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="lock is already held"):
            queue.run_queue(plan_path, output_dir, workers=1)
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_cli_returns_nonzero_for_any_nonterminal_queue_state(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        queue,
        "run_queue",
        lambda plan, output_dir, workers: {
            "counts": {"technical_failed": 1},
            "jobs": {},
        },
    )

    assert queue.main(
        ["--plan", str(tmp_path / "plan"), "--output-dir", str(tmp_path)]
    ) == 1
