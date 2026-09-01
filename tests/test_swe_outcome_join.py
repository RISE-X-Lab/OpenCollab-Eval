from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.commands.swe_outcome_join import (
    IDENTITY_FROM_INSTANCE_ID,
    IDENTITY_FROM_SIDECAR,
    JOIN_AMBIGUOUS,
    JOIN_MATCHED,
    JOIN_NOT_ATTEMPTED,
    JOIN_UNMATCHED,
    PredictionIndex,
    collect_outcomes,
    discover_report_dirs,
    main,
    summarize,
)


def _tests_status(success: list[str], failure: list[str], p2p_failure: list[str] | None = None) -> dict:
    return {
        "FAIL_TO_PASS": {"success": success, "failure": failure},
        "PASS_TO_PASS": {"success": ["t::kept"], "failure": p2p_failure or []},
    }


def _write_instance(
    work_dir: Path,
    run_id: str,
    model: str,
    instance_id: str,
    report_body: dict,
    sidecar: dict | None = None,
) -> Path:
    directory = work_dir / "logs" / "run_evaluation" / run_id / model.replace("/", "__") / instance_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps({instance_id: report_body}), encoding="utf-8")
    if sidecar is not None:
        (directory / "opencollab-attempt.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return directory


_PARTIAL_BODY = {
    "patch_exists": True,
    "patch_successfully_applied": True,
    "resolved": False,
    "tests_status": _tests_status(["t::a"], ["t::b", "t::c"]),
}
_CRASH_BODY = {
    "patch_exists": True,
    "patch_successfully_applied": False,
    "resolved": False,
    "infra_failure": True,
    "infra_failure_reason": "network_unreachable",
}
_RESOLVED_BODY = {
    "patch_exists": True,
    "patch_successfully_applied": True,
    "resolved": True,
    "tests_status": _tests_status(["t::a", "t::b"], []),
}


def test_discovery_walks_every_run_and_model_by_default(tmp_path: Path) -> None:
    _write_instance(tmp_path, "runA", "deepseek/x", "repo__repo-1", _PARTIAL_BODY)
    _write_instance(tmp_path, "runB", "deepseek/x", "repo__repo-1", _RESOLVED_BODY)
    found = discover_report_dirs(tmp_path)
    assert [(run, instance) for run, _model, instance, _path in found] == [
        ("runA", "repo__repo-1"),
        ("runB", "repo__repo-1"),
    ]
    only_b = discover_report_dirs(tmp_path, run_ids=["runB"])
    assert [run for run, _m, _i, _p in only_b] == ["runB"]
    # The model filter accepts the real name and matches the sanitized directory.
    assert discover_report_dirs(tmp_path, models=["deepseek/x"]) == found
    assert discover_report_dirs(tmp_path, models=["other/y"]) == []


def test_old_batch_is_back_filled_from_the_reports_alone(tmp_path: Path) -> None:
    _write_instance(tmp_path, "run1", "m", "repo__repo-1", _PARTIAL_BODY)
    rows = collect_outcomes(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["y"] == 1 / 3
    assert row["f2p_success_count"] == 1
    assert row["f2p_failure_count"] == 2
    assert row["f2p_denominator"] == 3
    assert row["resolved"] is False
    assert row["join_status"] == JOIN_NOT_ATTEMPTED


def test_a_crashed_run_lands_in_the_output_scored_zero_not_omitted(tmp_path: Path) -> None:
    _write_instance(tmp_path, "run1", "m", "repo__repo-1", _CRASH_BODY)
    (row,) = collect_outcomes(tmp_path)
    assert row["y"] == 0.0
    assert row["f2p_graded"] is False
    assert row["f2p_denominator"] == 0
    assert row["ungraded_reason"] == "patch_not_applied_or_log_unparsed"


def test_sidecar_identity_and_instance_id_fallback_are_labelled_apart(tmp_path: Path) -> None:
    """A row bound by record_id must never look as trustworthy as one guessed by task.

    Repetitions of the same task under one arm share an instance_id, so a
    fallback row cannot say which repetition it grades. Pooling the two
    provenances would silently attribute an outcome to the wrong run.
    """
    _write_instance(
        tmp_path,
        "run1",
        "m",
        "repo__repo-1",
        _PARTIAL_BODY,
        sidecar={"instance_id": "repo__repo-1", "record_id": "rec-1", "patch_sha256": "a" * 64, "status": "finished"},
    )
    _write_instance(tmp_path, "run1", "m", "repo__repo-2", _RESOLVED_BODY)
    bound, fallback = collect_outcomes(tmp_path)

    assert bound["identity_source"] == IDENTITY_FROM_SIDECAR
    assert bound["identity_trusted"] is True
    assert bound["record_id"] == "rec-1"
    assert bound["attempt_status"] == "finished"

    assert fallback["identity_source"] == IDENTITY_FROM_INSTANCE_ID
    assert fallback["identity_trusted"] is False
    assert fallback["record_id"] == ""

    summary = summarize([bound, fallback])
    assert summary["identity_from_sidecar"] == 1
    assert summary["identity_from_instance_id_fallback"] == 1


def test_join_prefers_the_record_id_carried_by_the_sidecar(tmp_path: Path) -> None:
    _write_instance(
        tmp_path,
        "run1",
        "m",
        "repo__repo-1",
        _PARTIAL_BODY,
        sidecar={"instance_id": "repo__repo-1", "record_id": "rec-2", "patch_sha256": "b" * 64},
    )
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"instance_id": "repo__repo-1", "record_id": "rec-1", "arm": "single"},
                {"instance_id": "repo__repo-1", "record_id": "rec-2", "arm": "team"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    index = PredictionIndex()
    index.add_file(predictions)
    (row,) = collect_outcomes(tmp_path, index=index, carry_fields=["arm"])
    assert row["join_key"] == "record_id"
    assert row["join_status"] == JOIN_MATCHED
    assert row["join_candidates"] == 1
    # Two rows share the instance id, so only the record id picks the right arm.
    assert row["prediction_arm"] == "team"
    assert row["prediction_line"] == 2


def test_join_falls_back_to_patch_digest_then_instance_id(tmp_path: Path) -> None:
    _write_instance(
        tmp_path,
        "run1",
        "m",
        "repo__repo-1",
        _PARTIAL_BODY,
        sidecar={"instance_id": "repo__repo-1", "record_id": "", "patch_sha256": "c" * 64},
    )
    _write_instance(tmp_path, "run1", "m", "repo__repo-2", _RESOLVED_BODY)
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"instance_id": "repo__repo-1", "patch_sha256": "c" * 64, "arm": "team"},
                {"instance_id": "repo__repo-2", "arm": "single"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    index = PredictionIndex()
    index.add_file(predictions)
    by_digest, by_instance = collect_outcomes(tmp_path, index=index, carry_fields=["arm"])
    assert by_digest["join_key"] == "patch_sha256"
    assert by_digest["identity_trusted"] is True
    assert by_instance["join_key"] == "instance_id"
    assert by_instance["join_status"] == JOIN_MATCHED
    # Matching on instance_id alone still does not upgrade the identity.
    assert by_instance["identity_trusted"] is False


def test_ambiguous_and_unmatched_joins_are_not_reported_as_matches(tmp_path: Path) -> None:
    _write_instance(tmp_path, "run1", "m", "repo__repo-1", _PARTIAL_BODY)
    _write_instance(tmp_path, "run1", "m", "repo__repo-9", _RESOLVED_BODY)
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"instance_id": "repo__repo-1", "record_id": "rec-1"},
                {"instance_id": "repo__repo-1", "record_id": "rec-2"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    index = PredictionIndex()
    index.add_file(predictions)
    ambiguous, unmatched = collect_outcomes(tmp_path, index=index, carry_fields=["record_id"])
    assert ambiguous["join_status"] == JOIN_AMBIGUOUS
    assert ambiguous["join_candidates"] == 2
    assert "prediction_record_id" not in ambiguous
    assert unmatched["join_status"] == JOIN_UNMATCHED
    assert unmatched["join_candidates"] == 0


def test_gold_denominator_check_reaches_the_rows(tmp_path: Path) -> None:
    _write_instance(tmp_path, "run1", "m", "repo__repo-1", _PARTIAL_BODY)
    checked = collect_outcomes(tmp_path, gold_fail_to_pass={"repo__repo-1": ["t::a", "t::b", "t::c", "t::skipped"]})
    assert checked[0]["gold_denominator_matches"] is False
    assert checked[0]["missing_from_denominator"] == 1
    unchecked = collect_outcomes(tmp_path)
    assert unchecked[0]["gold_denominator_matches"] is None


def test_summary_keeps_the_all_runs_mean_apart_from_the_graded_only_mean(tmp_path: Path) -> None:
    _write_instance(tmp_path, "run1", "m", "repo__repo-1", _RESOLVED_BODY)
    _write_instance(tmp_path, "run1", "m", "repo__repo-2", _CRASH_BODY)
    summary = summarize(collect_outcomes(tmp_path))
    assert summary["runs"] == 2
    assert summary["graded"] == 1
    assert summary["ungraded"] == 1
    # The paper's y is defined over every run: the crashed run contributes 0.
    assert summary["mean_y_over_all_runs"] == 0.5
    assert summary["mean_y_over_graded_runs"] == 1.0
    assert summary["resolved"] == 1
    assert summary["ungraded_reasons"] == {"patch_not_applied_or_log_unparsed": 1}


def test_main_writes_one_row_per_run_and_prints_the_summary(tmp_path: Path, capsys) -> None:
    work_dir = tmp_path / "eval"
    _write_instance(
        work_dir,
        "run1",
        "deepseek/x",
        "repo__repo-1",
        _PARTIAL_BODY,
        sidecar={"instance_id": "repo__repo-1", "record_id": "rec-1", "patch_sha256": "d" * 64},
    )
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text(json.dumps({"instance_id": "repo__repo-1", "record_id": "rec-1", "arm": "team"}) + "\n")
    output = tmp_path / "out" / "outcomes.jsonl"
    code = main(
        [
            "--work-dir",
            str(work_dir),
            "--predictions",
            str(predictions),
            "--carry",
            "arm",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["schema"] == "opencollab.swe_outcome.v1"
    assert rows[0]["y"] == 1 / 3
    assert rows[0]["prediction_arm"] == "team"
    summary = json.loads(capsys.readouterr().out)
    assert summary["runs"] == 1
    assert summary["join_status_counts"] == {JOIN_MATCHED: 1}


def test_main_reports_a_broken_predictions_file_instead_of_scoring_zero(tmp_path: Path, capsys) -> None:
    work_dir = tmp_path / "eval"
    _write_instance(work_dir, "run1", "m", "repo__repo-1", _PARTIAL_BODY)
    predictions = tmp_path / "preds.jsonl"
    predictions.write_text("{not json\n", encoding="utf-8")
    code = main(["--work-dir", str(work_dir), "--predictions", str(predictions), "--output", str(tmp_path / "o.jsonl")])
    assert code == 2
    assert "invalid JSONL" in capsys.readouterr().err


def test_missing_work_dir_yields_no_rows_rather_than_raising(tmp_path: Path) -> None:
    assert collect_outcomes(tmp_path / "absent") == []
    assert summarize([])["mean_y_over_all_runs"] is None
