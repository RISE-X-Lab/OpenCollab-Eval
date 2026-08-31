from __future__ import annotations

from swe_eval_status_support import (
    _patch,
    _write_jsonl,
    build_snapshots,
    discover_eval_reports,
    discovery_mod,
    importlib,
    json,
    os,
    pytest,
    row_patch_sha,
    signal,
    subprocess,
    time,
)

from opencollab_eval.engine import swe_eval_records


def test_build_snapshots_binds_legacy_prediction_record_id_alias(tmp_path):
    """Do not reuse a report from another attempt when ``attempt_id`` is used."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    task = "task-1"
    patch = _patch("+alias\n")
    current_sha = row_patch_sha({"model_patch": patch})
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [{"instance_id": task, "attempt_id": "current", "model_patch": patch}],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "attempt_id": "current",
                "patch_sha256": current_sha,
                "workflow_status": "done",
            }
        ],
    )
    side_dir = run_dir / "official_eval_auto"
    side_dir.mkdir()
    (side_dir / "summary.json").write_text(
        json.dumps(
            {
                task: {
                    "resolved": True,
                    "patch_sha256": current_sha,
                    "record_id": "old-attempt",
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = build_snapshots(run_dir)[0]

    assert snapshot.metric_pairing == "record_id"
    assert snapshot.eval_summary.done_count == 0
    assert snapshot.eval_summary.ignored_patch_mismatch_count == 1


@pytest.mark.parametrize("field", ["resolved_instances", "unresolved_instances"])
def test_discovery_ignores_summary_with_boolean_count(tmp_path, field):
    payload = {
        "instance_id": "task-1",
        "status": "done",
        "resolved_instances": 0,
        "unresolved_instances": 0,
        "patch_sha256": "a" * 64,
    }
    payload[field] = True
    (tmp_path / "invalid.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    assert discover_eval_reports(tmp_path) == []


def test_discovery_rejects_verdict_count_mismatch():
    payload = {
        "status": "done",
        "resolved": True,
        "resolved_instances": 0,
        "unresolved_instances": 1,
    }
    with pytest.raises(ValueError, match="verdict"):
        discovery_mod._status_from_summary_payload(payload)


def test_discovery_rejects_conflicting_attempt_task_aliases(tmp_path):
    payload = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": "task-1",
        "task_id": "task-2",
        "record_id": "record-1",
        "patch_sha256": "a" * 64,
        "started_at_ns": 1,
    }

    assert (
        discovery_mod._attempt_from_payload(tmp_path / "attempt.json", payload)
        is None
    )


@pytest.mark.parametrize("field_value", [True, 1.9, "1.9"])
def test_discovery_rejects_lossy_attempt_numeric_fields(tmp_path, field_value):
    payload = {
        "schema": "opencollab.swe_eval_attempt.v1",
        "instance_id": "task-1",
        "record_id": "record-1",
        "patch_sha256": "a" * 64,
        "started_at_ns": field_value,
    }

    assert discovery_mod._attempt_from_payload(tmp_path / "attempt.json", payload) is None


def test_record_alias_conflicts_are_not_resolved_by_field_order():
    assert swe_eval_records.row_task_id(
        {"instance_id": "task-a", "task_id": "task-b"}
    ) == ""
    assert swe_eval_records.row_record_id(
        {"record_id": "record-a", "attempt_id": "record-b"}
    ) == ""
    assert swe_eval_records.row_explicit_patch_sha(
        {"patch_sha256": "a" * 64, "patch_sha": "b" * 64}
    ) == ""


def test_expired_claim_retains_live_group_when_identity_probe_is_unknown(
    monkeypatch, tmp_path
):
    runner = importlib.import_module(
        "opencollab_eval.commands.run_swebench_eval_per_instance"
    )
    process = subprocess.Popen(
        [
            runner.sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable, "
            "'-c', 'import time; time.sleep(30)']); time.sleep(.2)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    work_dir = tmp_path / "eval"
    identity = {
        "instance_id": "task-1",
        "record_id": "r1",
        "patch_sha256": "a" * 64,
    }
    claim_path = runner._claim_path(work_dir, "task-1")
    try:
        expected = runner.process_start_identity(process.pid)
        time_limit = time.monotonic() + 2
        while not runner._process_group_exists(process.pid) and time.monotonic() < time_limit:
            runner.time.sleep(0.01)
        process.wait(timeout=2)
        monkeypatch.setattr(runner, "process_start_identity", lambda _pid: "")
        runner._write_json_atomic(
            claim_path,
            {
                "schema": "opencollab.swe_eval_claim.v1",
                **identity,
                "owner_token": "old-owner",
                "status": "cleanup_failed",
                "lease_until_ns": 1,
                "evaluator_pgid": process.pid,
                "evaluator_start_identity": expected or "proc:expected",
            },
        )
        acquired, returned_path = runner.acquire_claim(
            work_dir, "task-1", identity, lease_seconds=10, owner_token="new-owner"
        )
        retained = json.loads(claim_path.read_text(encoding="utf-8"))
        assert acquired is False
        assert returned_path == claim_path
        assert retained["owner_token"] == "old-owner"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()


def test_auto_eval_binds_changed_legacy_report_with_patch_but_no_record(tmp_path):
    """A rewritten batch result inherits the matching attempt record identity."""
    side_dir = tmp_path / "official_eval_auto"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    report_path = side_dir / "summary.json"
    patch_sha = "a" * 64
    report_path.write_text(
        json.dumps({"task-1": {"resolved": False, "patch_sha256": patch_sha}}),
        encoding="utf-8",
    )
    prior_fingerprint = discovery_mod._report_stat_fingerprint(report_path.stat())
    started_at_ns = time.time_ns()
    (attempt_dir / "current.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "started_at_ns": started_at_ns,
                "status": "completed",
                "pid": 0,
                "evaluator_pgid": 0,
                "prior_reports": {"summary.json": prior_fingerprint},
            }
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps({"task-1": {"resolved": True, "patch_sha256": patch_sha}}),
        encoding="utf-8",
    )
    changed_ns = max(time.time_ns(), started_at_ns + 1)
    os.utime(report_path, ns=(changed_ns, changed_ns))

    reports = discovery_mod.discover_eval_reports(side_dir)

    assert len(reports) == 1
    assert reports[0].record_id == "r1"
    assert reports[0].status == "done"
    assert reports[0].resolved_count == 1


def test_auto_eval_binds_new_legacy_report_with_patch_but_no_record(tmp_path):
    """A report absent from the prior fingerprint set is new attempt evidence."""
    side_dir = tmp_path / "official_eval_auto"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    patch_sha = "a" * 64
    started_at_ns = time.time_ns()
    (attempt_dir / "current.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "started_at_ns": started_at_ns,
                "status": "completed",
                "pid": 0,
                "evaluator_pgid": 0,
                "prior_reports": {},
            }
        ),
        encoding="utf-8",
    )
    report_path = side_dir / "new-summary.json"
    report_path.write_text(
        json.dumps({"task-1": {"resolved": True, "patch_sha256": patch_sha}}),
        encoding="utf-8",
    )
    changed_ns = max(time.time_ns(), started_at_ns + 1)
    os.utime(report_path, ns=(changed_ns, changed_ns))

    reports = discovery_mod.discover_eval_reports(side_dir)

    assert len(reports) == 1
    assert reports[0].record_id == "r1"
    assert reports[0].status == "done"


def test_auto_eval_does_not_bind_unchanged_legacy_report_with_patch_but_no_record(
    tmp_path,
):
    """An unchanged pre-attempt result must still produce a technical sidecar result."""
    side_dir = tmp_path / "official_eval_auto"
    attempt_dir = side_dir / ".opencollab" / "attempts"
    attempt_dir.mkdir(parents=True)
    report_path = side_dir / "summary.json"
    patch_sha = "a" * 64
    report_path.write_text(
        json.dumps({"task-1": {"resolved": True, "patch_sha256": patch_sha}}),
        encoding="utf-8",
    )
    prior_fingerprint = discovery_mod._report_stat_fingerprint(report_path.stat())
    started_at_ns = time.time_ns()
    (attempt_dir / "current.json").write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_eval_attempt.v1",
                "instance_id": "task-1",
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "started_at_ns": started_at_ns,
                "status": "completed",
                "pid": 0,
                "evaluator_pgid": 0,
                "prior_reports": {"summary.json": prior_fingerprint},
            }
        ),
        encoding="utf-8",
    )

    reports = discovery_mod.discover_eval_reports(side_dir)
    summary = discovery_mod.summarize_eval_reports(
        reports,
        task_id="task-1",
        current_patch_sha=patch_sha,
        current_record_id="r1",
    )

    assert any(report.record_id == "" and report.status == "done" for report in reports)
    assert summary.done_count == 0
    assert summary.failed_count == 1
