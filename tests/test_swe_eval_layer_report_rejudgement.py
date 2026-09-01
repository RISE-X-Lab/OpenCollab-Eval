from test_swe_eval_layer_report import (
    _assert_technical,
    _load_module,
    _row,
    _sha,
    _write_json,
)


def test_evidence_only_rejudgement_shares_source_round_regardless_filename_order(tmp_path):
    module = _load_module()
    task = "instance_owner__repo-82"
    parent_row = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    executed_row = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    executed_row["eval"]["attempt_count"] = 2
    derived_row = _row(82, task, "/run/task.log", 10, "eval_done", True)
    derived_row["eval"].update(executed=False, attempt_count=2)
    image_id = f"sha256:{'d' * 64}"
    for row in (parent_row, executed_row, derived_row):
        row["eval"]["summary"]["eval_image_id"] = image_id
    identity = {
        "task": task,
        "record_id": f"record-{task}",
        "patch_sha256": _sha(task),
        "eval_patch_sha256": _sha(task),
        "eval_spec_sha256": "e" * 64,
        "eval_image_id": image_id,
    }
    derived_row["eval"]["summary"]["rejudgement"] = {
        "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
        "matching_eval_attempts": 2,
        "added_eval_attempts": 0,
        "attempt_identity": identity,
    }
    parent = _write_json(tmp_path / "m_parallel_summary.json", {"rows": [parent_row]})
    executed = _write_json(
        tmp_path / "z_task_82_eval_only_executed.json",
        {"rows": [executed_row]},
    )
    derived = _write_json(
        tmp_path / "a_task_82_eval_only_rejudged.json",
        {
            "rejudgement": {"schema": "opencollab.eval_only_reconciliation.v1"},
            "rows": [derived_row],
        },
    )

    report = module.build_report([parent, executed, derived], max_rounds=2)

    assert report["counts"]["resolved"] == 1
    assert report["counts"]["technical_failed_final"] == 0
    assert report["counts"]["rounds"] == 2
    assert report["tasks"][0]["resolved"] is True
    assert report["tasks"][0]["eval_attempt_count"] == 2


def test_evidence_only_rejudgement_rejects_mismatched_attempt_count(tmp_path):
    module = _load_module()
    task = "instance_owner__repo-82"
    executed_row = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    derived_row = _row(82, task, "/run/task.log", 10, "eval_done", True)
    derived_row["eval"].update(executed=False, attempt_count=2)
    image_id = f"sha256:{'d' * 64}"
    for row in (executed_row, derived_row):
        row["eval"]["summary"]["eval_image_id"] = image_id
    derived_row["eval"]["summary"]["rejudgement"] = {
        "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
        "matching_eval_attempts": 1,
        "added_eval_attempts": 0,
        "attempt_identity": {
            "task": task,
            "record_id": f"record-{task}",
            "patch_sha256": _sha(task),
            "eval_patch_sha256": _sha(task),
            "eval_spec_sha256": "e" * 64,
            "eval_image_id": image_id,
        },
    }
    executed = _write_json(tmp_path / "executed.json", {"rows": [executed_row]})
    derived = _write_json(
        tmp_path / "derived.json",
        {
            "rejudgement": {"schema": "opencollab.eval_only_reconciliation.v1"},
            "rows": [derived_row],
        },
    )

    report = module.build_report([derived, executed], max_rounds=2)

    _assert_technical(report, "orphan_evidence_only_rejudgement")
    assert report["tasks"][0]["eval_attempt_count"] == 1


def test_orphan_evidence_only_rejudgement_is_technical(tmp_path):
    module = _load_module()
    task = "instance_owner__repo-82"
    parent_row = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    parent_row["eval"].update(executed=False, attempt_count=0)
    derived_row = _row(82, task, "/run/task.log", 10, "eval_done", True)
    derived_row["eval"].update(executed=False, attempt_count=0)
    image_id = f"sha256:{'d' * 64}"
    for row in (parent_row, derived_row):
        row["eval"]["summary"]["eval_image_id"] = image_id
    derived_row["eval"]["summary"]["rejudgement"] = {
        "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
        "matching_eval_attempts": 1,
        "added_eval_attempts": 0,
        "attempt_identity": {
            "task": task,
            "record_id": f"record-{task}",
            "patch_sha256": _sha(task),
            "eval_patch_sha256": _sha(task),
            "eval_spec_sha256": "e" * 64,
            "eval_image_id": image_id,
        },
    }
    parent = _write_json(tmp_path / "parallel_summary.json", {"rows": [parent_row]})
    derived = _write_json(
        tmp_path / "task_82_eval_only_rejudged.json",
        {
            "rejudgement": {"schema": "opencollab.eval_only_reconciliation.v1"},
            "rows": [derived_row],
        },
    )

    report = module.build_report([parent, derived], max_rounds=2)

    _assert_technical(report, "orphan_evidence_only_rejudgement")
    assert report["tasks"][0]["eval_attempt_count"] == 0


def test_disguised_third_eval_cannot_bypass_round_limit(tmp_path):
    module = _load_module()
    task = "instance_owner__repo-82"
    first = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    second = _row(82, task, "/run/task.log", 10, "technical_eval_failed")
    second["eval"]["attempt_count"] = 2
    disguised = _row(82, task, "/run/task.log", 10, "eval_done", True)
    disguised["eval"].update(executed=False, attempt_count=3)
    disguised["eval"]["summary"]["rejudgement"] = {
        "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
        "matching_eval_attempts": 3,
        "added_eval_attempts": 0,
        "attempt_identity": {},
    }
    paths = [
        _write_json(tmp_path / "parallel_summary.json", {"rows": [first]}),
        _write_json(tmp_path / "task_82_eval_only_executed.json", {"rows": [second]}),
        _write_json(
            tmp_path / "task_82_eval_only_rejudged.json",
            {
                "rejudgement": {"schema": "opencollab.eval_only_reconciliation.v1"},
                "rows": [disguised],
            },
        ),
    ]

    report = module.build_report(paths, max_rounds=2)

    _assert_technical(report, "orphan_evidence_only_rejudgement")
    assert report["tasks"][0]["eval_attempt_count"] == 2
    assert report["counts"]["rounds"] == 3


def test_build_report_accepts_a_top_level_legacy_mirror(tmp_path):
    """A top-level row mirrored by results remains one valid attempt."""
    module = _load_module()
    task = "instance_owner__repo-82"
    row = _row(82, task, "/run/task.log", 10, "eval_done", True)
    report_path = _write_json(
        tmp_path / "hybrid.json",
        {
            "schema": "opencollab.swe_parallel_runner.v2",
            "indices": [82],
            "rows": [row],
            "results": [
                {
                    "index": 82,
                    "completed": True,
                    "runner_status": "done",
                    "rows": [row],
                }
            ],
        },
    )

    report = module.build_report([report_path], expected_indices=[82])

    assert report["counts"]["eval_success"] == 1
    assert report["counts"]["technical_failed_final"] == 0
    assert report["tasks"][0]["eval_attempt_count"] == 1


def test_build_report_retains_distinct_nested_duplicate_rows_as_technical(
    tmp_path,
):
    """Two nested result ledgers must retain duplicate detection semantics."""
    module = _load_module()
    task = "instance_owner__repo-82"
    row = _row(82, task, "/run/task.log", 10, "eval_done", True)
    report_path = _write_json(
        tmp_path / "nested-duplicates.json",
        {
            "schema": "opencollab.swe_parallel_runner.v2",
            "indices": [82],
            "results": [
                {"index": 82, "completed": True, "runner_status": "done", "rows": [row]},
                {"index": 82, "completed": True, "runner_status": "done", "rows": [row]},
            ],
        },
    )

    report = module.build_report([report_path], expected_indices=[82])

    assert report["counts"]["technical_failed_final"] == 1
    assert "duplicate_task_row" in report["tasks"][0]["technical_reasons"]
