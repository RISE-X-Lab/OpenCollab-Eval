from __future__ import annotations

from swe_eval_status_support import (
    _patch,
    _write_jsonl,
    build_snapshots,
    discover_eval_reports,
    discovery_mod,
    json,
    pytest,
    row_patch_sha,
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
