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
