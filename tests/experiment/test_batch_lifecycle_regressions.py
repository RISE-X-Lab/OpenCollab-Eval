"""Regression tests for planned batch identity and report selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.commands.batch_reporting import select_cell_rows
from opencollab_eval.experiment.batch_spec import SpecError, load_spec, spec_identity
from tests.experiment.batch_support import experiment as experiment
from tests.experiment.batch_support import oc_repo as oc_repo
from tests.experiment.test_batch_replacement import _metrics, _plan, _replacement_spec, _retry_spec, _run


def _record(experiment: dict, name: str) -> Path:
    return Path(experiment["dir"]).parent / "batches" / f"{name}.launch" / "batch.json"


def test_changed_spec_cannot_overwrite_a_planned_name_or_its_inputs(experiment: dict, capsys) -> None:
    spec = Path(experiment["spec"])
    assert _plan(experiment, spec) == 0
    record_path = _record(experiment, "t1")
    original = json.loads(record_path.read_text(encoding="utf-8"))
    original["launches"] = [{"at": "2026-09-01T00:00:00+00:00", "argv": ["paid-run"]}]
    record_path.write_text(json.dumps(original), encoding="utf-8")
    inputs = record_path.parent / original["instances"]["file"]
    before_record, before_inputs = record_path.read_bytes(), inputs.read_bytes()

    spec.write_text(
        spec.read_text(encoding="utf-8").replace("budget_per_seat: 2000000", "budget_per_seat: 1000000"),
        encoding="utf-8",
    )
    assert _plan(experiment, spec) == 2
    assert "t1" in capsys.readouterr().err
    assert record_path.read_bytes() == before_record
    assert inputs.read_bytes() == before_inputs


def test_changed_frame_cannot_overwrite_inputs_under_the_same_spec(experiment: dict) -> None:
    spec = Path(experiment["spec"])
    assert _plan(experiment, spec) == 0
    record_path = _record(experiment, "t1")
    inputs = record_path.parent / "t1-instances.jsonl"
    before_record, before_inputs = record_path.read_bytes(), inputs.read_bytes()
    frame = Path(experiment["frame"])
    frame.write_text(frame.read_text(encoding="utf-8").replace("fix a__a-1", "changed a__a-1"), encoding="utf-8")

    assert _plan(experiment, spec) == 2
    assert record_path.read_bytes() == before_record
    assert inputs.read_bytes() == before_inputs


def test_modified_existing_inputs_are_kept_for_investigation(experiment: dict) -> None:
    spec = Path(experiment["spec"])
    assert _plan(experiment, spec) == 0
    record_path = _record(experiment, "t1")
    inputs = record_path.parent / "t1-instances.jsonl"
    inputs.write_bytes(inputs.read_bytes() + b"modified\n")
    before_record, before_inputs = record_path.read_bytes(), inputs.read_bytes()

    assert _plan(experiment, spec) == 2
    assert record_path.read_bytes() == before_record
    assert inputs.read_bytes() == before_inputs


def test_record_spec_is_checked_even_when_its_digest_is_stale(experiment: dict) -> None:
    spec = Path(experiment["spec"])
    assert _plan(experiment, spec) == 0
    record_path = _record(experiment, "t1")
    old = json.loads(record_path.read_text(encoding="utf-8"))
    old["spec"]["budget_per_seat"] = 1000000
    record_path.write_text(json.dumps(old), encoding="utf-8")
    before = record_path.read_bytes()

    assert _plan(experiment, spec) == 2
    assert record_path.read_bytes() == before


def test_same_spec_replan_keeps_launches_and_allows_concurrency_change(experiment: dict) -> None:
    spec = Path(experiment["spec"])
    assert _plan(experiment, spec) == 0
    record_path = _record(experiment, "t1")
    old = json.loads(record_path.read_text(encoding="utf-8"))
    old["launches"] = [{"at": "2026-09-01T00:00:00+00:00", "argv": ["paid-run"]}]
    record_path.write_text(json.dumps(old), encoding="utf-8")
    spec.write_text(spec.read_text(encoding="utf-8").replace("concurrency: 2", "concurrency: 3"), encoding="utf-8")

    assert _plan(experiment, spec) == 0
    new = json.loads(record_path.read_text(encoding="utf-8"))
    assert new["spec_digest"] == old["spec_digest"]
    assert new["launches"] == old["launches"]
    assert new["spec"]["concurrency"] == 3


def test_replacement_target_is_unique_during_plan_and_report(experiment: dict, capsys) -> None:
    exp = Path(experiment["dir"])
    suite = exp / "suite" / "tiny.csv"
    suite.write_text(suite.read_text(encoding="utf-8") + "4,d__d-4,d/d,>4 hours,img/d-4:latest\n", encoding="utf-8")
    frame = Path(experiment["frame"])
    frame.write_text(
        frame.read_text(encoding="utf-8")
        + json.dumps({"instance_id": "d__d-4", "repo": "d/d", "problem_statement": "fix d"})
        + "\n",
        encoding="utf-8",
    )
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    first = _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")
    second = _replacement_spec(experiment, "t1y", "rows: {start: 4, stop: 4}", "b__b-2")
    assert _plan(experiment, first) == 0
    assert _plan(experiment, second) == 2
    assert "t1x" in capsys.readouterr().err
    assert not _record(experiment, "t1y").exists()

    # A duplicate record from an earlier version must be refused by reporting too.
    second_record = _record(experiment, "t1y")
    second_record.parent.mkdir(parents=True)
    second_record.write_text(json.dumps({"spec": spec_identity(load_spec(second))}), encoding="utf-8")
    root = exp.parent / "batches"
    _metrics(root / "t1", [_run("a__a-1"), _run("b__b-2")])
    _metrics(root / "t1x", [_run("c__c-3")])
    _metrics(root / "t1y", [_run("d__d-4")])
    with pytest.raises(SpecError, match="b__b-2"):
        select_cell_rows(batch_cli.Batch(base, exp))
    report = exp.parent / "report.json"
    assert (
        batch_cli.main(
            ["--experiment-dir", str(exp), "report", str(base), "--scanner", "none", "--json", str(report)],
            remote_factory=lambda h: None,
        )
        == 2
    )
    assert not report.exists()


def test_distinct_replacement_targets_keep_the_original_denominator(experiment: dict) -> None:
    exp = Path(experiment["dir"])
    suite = exp / "suite" / "tiny.csv"
    suite.write_text(suite.read_text(encoding="utf-8") + "4,d__d-4,d/d,>4 hours,img/d-4:latest\n", encoding="utf-8")
    frame = Path(experiment["frame"])
    frame.write_text(
        frame.read_text(encoding="utf-8")
        + json.dumps({"instance_id": "d__d-4", "repo": "d/d", "problem_statement": "fix d"})
        + "\n",
        encoding="utf-8",
    )
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    assert _plan(experiment, _replacement_spec(experiment, "t1x", "rows: {start: 3, stop: 3}", "b__b-2")) == 0
    assert _plan(experiment, _replacement_spec(experiment, "t1y", "rows: {start: 4, stop: 4}", "a__a-1")) == 0
    root = exp.parent / "batches"
    _metrics(root / "t1", [_run("a__a-1"), _run("b__b-2")])
    _metrics(root / "t1x", [_run("c__c-3")])
    _metrics(root / "t1y", [_run("d__d-4")])

    selected = select_cell_rows(batch_cli.Batch(base, exp))
    assert [row.instance_id for row in selected.rows] == ["c__c-3", "d__d-4"]
    assert {row.instance_id for row, _ in selected.excluded} == {"a__a-1", "b__b-2"}


def test_nested_retry_is_selected_with_all_attempt_usage(experiment: dict, capsys) -> None:
    exp = Path(experiment["dir"])
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    first = _retry_spec(experiment, "t1r1", "t1", "rows: {start: 2, stop: 2}", "tiny")
    second = _retry_spec(experiment, "t1r2", "t1r1", "rows: {start: 2, stop: 2}", "tiny")
    assert _plan(experiment, first) == 0
    assert _plan(experiment, second) == 0
    root = exp.parent / "batches"
    _metrics(root / "t1", [_run("a__a-1", tokens=10), _run("b__b-2", "failed", 1, "APIError")])
    _metrics(root / "t1r1", [_run("b__b-2", "failed", 2, "APIError")])
    _metrics(root / "t1r2", [_run("b__b-2", "completed", 3)])

    rows = select_cell_rows(batch_cli.Batch(base, exp)).rows
    chosen = next(row for row in rows if row.instance_id == "b__b-2")
    assert (chosen.source_batch, chosen.valid, chosen.attempt, chosen.attempts) == ("t1r2", True, 3, 3)
    assert chosen.attempt_tokens_total == 6
    report = exp.parent / "report.json"
    assert (
        batch_cli.main(
            ["--experiment-dir", str(exp), "report", str(base), "--scanner", "none", "--json", str(report)],
            remote_factory=lambda h: None,
        )
        == 0
    )
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["tokens_all_attempts"] == 16
    assert "merged retry: t1r2" in capsys.readouterr().out


def test_retry_cycle_is_rejected_from_the_root_report(experiment: dict) -> None:
    exp = Path(experiment["dir"])
    base = Path(experiment["spec"])
    assert _plan(experiment, base) == 0
    assert _plan(experiment, _retry_spec(experiment, "t1r1", "t1", "rows: {start: 2, stop: 2}", "tiny")) == 0
    assert _plan(experiment, _retry_spec(experiment, "t1r2", "t1r1", "rows: {start: 2, stop: 2}", "tiny")) == 0
    record_path = _record(experiment, "t1")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["spec"]["retry_of"] = "t1r2"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    _metrics(exp.parent / "batches" / "t1", [_run("a__a-1"), _run("b__b-2")])
    with pytest.raises(SpecError, match="cycle"):
        select_cell_rows(batch_cli.Batch(base, exp))
