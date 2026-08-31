from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_swe_rejudge_queue_reliability import (
    _accept_terminal,
    _plan,
    _terminal_report,
)

from opencollab_eval.commands import swe_rejudge_queue as queue


def test_queue_refresh_filters_late_other_candidate_verdict(
    tmp_path, monkeypatch
):
    """Parent refresh must bind same-index history to the planned candidate."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    job = queue._read_plan(plan)["jobs"][0]
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
                ]
            }
        ),
        encoding="utf-8",
    )
    late_other = parent / "task_25_eval_only_late_other.json"
    _terminal_report(
        late_other,
        index=25,
        patch_sha256="b" * 64,
        resolved=False,
    )
    os.utime(late_other, ns=(9, 9))
    child_reports: list[Path] = []

    def fake_run(argv, *, log, timeout):
        del log, timeout
        output = Path(argv[argv.index("--json-output") + 1])
        _terminal_report(
            output,
            index=25,
            patch_sha256=job["source_patch_sha256"],
            resolved=True,
        )
        child_reports.append(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue, "_run_bounded_child", fake_run)
    refreshes: list[SimpleNamespace] = []
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: refreshes.append(args) or {"status": "done"},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"terminal": 1}
    assert len(refreshes) == 1
    identities = refreshes[0].candidate_identities
    assert identities == {
        25: (
            job["task"],
            job["record_id"],
            job["source_patch_sha256"],
            job["eval_patch_sha256"],
        )
    }
    from opencollab_eval.commands.swe_v1_prolite_report import (
        eval_only_reconciliation_reports,
    )

    assert eval_only_reconciliation_reports(
        parent,
        child_reports[0],
        candidate_identities=identities,
    ) == child_reports

def test_queue_accepts_recomputed_eval_hash_for_same_candidate(tmp_path, monkeypatch):
    """A derived eval-hash change must not force a duplicate official run."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    value = json.loads(plan.read_text(encoding="utf-8"))
    job = value["jobs"][0]
    job["eval_patch_sha256"] = "b" * 64
    plan.write_text(json.dumps(value), encoding="utf-8")

    existing = parent / "task_25_eval_only_recomputed.json"
    _terminal_report(existing, index=25, patch_sha256=job["source_patch_sha256"], resolved=True)
    payload = json.loads(existing.read_text(encoding="utf-8"))
    payload["rows"][0]["generation"]["eval_patch_sha256"] = "c" * 64
    existing.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        queue,
        "_run_bounded_child",
        lambda *args, **kwargs: pytest.fail("recomputed candidate should be terminal"),
    )
    refreshes: list[SimpleNamespace] = []
    monkeypatch.setattr(
        queue,
        "update_parent_fact_report",
        lambda args: refreshes.append(args) or {"status": "done"},
    )

    result = queue.run_queue(plan, tmp_path / "state", workers=1)

    assert result["counts"] == {"skipped_terminal": 1}
    assert len(refreshes) == 1
    assert refreshes[0].candidate_identities == {
        25: (
            job["task"],
            job["record_id"],
            job["source_patch_sha256"],
            job["eval_patch_sha256"],
        )
    }

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


@pytest.mark.parametrize("alias", ["task", "instance_id", "task_id"])
def test_queue_accepts_legacy_task_identity_aliases(tmp_path, monkeypatch, alias):
    """Legacy rows remain reusable when their canonical task alias is present."""
    _accept_terminal(monkeypatch)
    plan, parent = _plan(tmp_path)
    job = queue._read_plan(plan)["jobs"][0]
    row = {
        "index": job["index"],
        alias: job["task"],
        "generation": {
            "record_id": job["record_id"],
            "patch_sha256": job["source_patch_sha256"],
            "source_patch_sha256": job["source_patch_sha256"],
            "eval_patch_sha256": job["eval_patch_sha256"],
        },
        "eval": {"status": "eval_done", "summary": {"resolved": True}},
    }
    report = parent / "parallel_summary.json"
    report.write_text(json.dumps({"rows": [row]}), encoding="utf-8")

    assert queue._candidate_identity_status(job) == "verified"
    assert queue._terminal_report(job) == (report, "verified")


def test_queue_rejects_conflicting_legacy_task_aliases(tmp_path):
    """Conflicting task aliases must not silently choose one identity."""
    plan, parent = _plan(tmp_path)
    job = queue._read_plan(plan)["jobs"][0]
    row = {
        "index": job["index"],
        "task": job["task"],
        "instance_id": "instance_owner__other-25",
        "generation": {
            "record_id": job["record_id"],
            "patch_sha256": job["source_patch_sha256"],
            "source_patch_sha256": job["source_patch_sha256"],
            "eval_patch_sha256": job["eval_patch_sha256"],
        },
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"rows": [row]}), encoding="utf-8"
    )

    assert queue._row_identity(row) is None
    assert queue._candidate_identity_status(job) == "candidate_identity_missing"
    assert queue._terminal_report(job) == (None, "missing")
