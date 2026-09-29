"""Identify batch scoring controls from the harness's model and run ID."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from opencollab_eval.commands.batch_scoring import cmd_score_report


def _report(
    ids: tuple[str, ...] = ("case-a",),
    *,
    resolved_ids: tuple[str, ...] | None = None,
    submitted_ids: tuple[str, ...] | None = None,
) -> str:
    resolved = list(ids if resolved_ids is None else resolved_ids)
    submitted = list(ids if submitted_ids is None else submitted_ids)
    return json.dumps(
        {
            "total_instances": len(ids),
            "submitted_instances": len(submitted),
            "completed_instances": len(ids),
            "submitted_ids": submitted,
            "completed_ids": list(ids),
            "incomplete_ids": [],
            "resolved_ids": resolved,
            "unresolved_ids": [instance_id for instance_id in ids if instance_id not in resolved],
            "error_ids": [],
            "empty_patch_ids": [],
        }
    )


class ReportRemote:
    def __init__(self, reports: dict[str, str]) -> None:
        self.reports = reports

    def run(self, script: str) -> str:
        argv = shlex.split(script)
        if argv[:2] == ["ls", "-1"]:
            return "\n".join(self.reports) + "\n"
        if argv[0] == "cat":
            return self.reports[argv[1].rsplit("/", 1)[-1]]
        raise AssertionError(script)


def _score_report(
    reports: dict[str, str], *, batch_name: str = "gold-cell", ids: tuple[str, ...] = ("case-a",)
) -> int:
    batch = SimpleNamespace(
        spec=SimpleNamespace(name=batch_name),
        host=SimpleNamespace(workdir="/tmp/scoring-workdir"),
        rows=[{"instance_id": instance_id} for instance_id in ids],
    )
    return cmd_score_report(batch, ReportRemote(reports))


def test_normal_report_with_gold_in_batch_name_is_not_a_control(capsys) -> None:
    assert _score_report({"model.gold-cell-20260929.json": _report()}) == 1
    output = capsys.readouterr().out
    assert "model.gold-cell-20260929.json: resolved 1/1" in output
    assert "no gold control" in output


def test_harness_named_gold_report_is_a_control(capsys) -> None:
    assert _score_report({"gold.gold-cell-gold-20260929.json": _report()}) == 0
    assert "[ok  ] gold.gold-cell-gold-20260929.json" in capsys.readouterr().out


def test_mixed_reports_use_only_the_gold_model_and_gold_run(capsys) -> None:
    reports = {
        "model.gold-cell-20260929.json": _report(),
        "model.gold-cell-gold-20260929.json": _report(),
        "gold.gold-cell-20260929.json": _report(),
        "gold.gold-cell-gold-20260929.json": _report(resolved_ids=()),
    }
    assert _score_report(reports) == 1
    output = capsys.readouterr().out
    assert output.count("[FAIL]") == 1
    assert output.count(": resolved 1/1") == 3
    assert "the control failed" in output


@pytest.mark.parametrize(
    ("older_resolved", "newer_resolved", "expected"),
    [(False, True, 0), (True, False, 1)],
)
def test_latest_dated_gold_control_keeps_existing_verdict_order(
    older_resolved: bool, newer_resolved: bool, expected: int
) -> None:
    reports = {
        "gold.gold-cell-gold-20260928.json": _report(resolved_ids=None if older_resolved else ()),
        "gold.gold-cell-gold-20260929.json": _report(resolved_ids=None if newer_resolved else ()),
    }
    assert _score_report(reports) == expected


def test_no_gold_control_in_ordinary_batch(capsys) -> None:
    assert _score_report({"model.cell-20260929.json": _report()}, batch_name="cell") == 1
    assert "no gold control" in capsys.readouterr().out


def test_gold_and_model_reports_cover_the_current_batch(capsys) -> None:
    ids = ("case-a", "case-b")
    reports = {
        "gold.cell-gold-20260928.json": _report(ids),
        "model.cell-20260929.json": _report(ids, resolved_ids=("case-a",)),
    }
    assert _score_report(reports, batch_name="cell", ids=ids) == 0
    assert "[ok  ] gold.cell-gold-20260928.json" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("gold_report", "expected_detail"),
    [
        (_report(("case-a",)), "report totals"),
        (_report(("case-a", "case-x")), "outside the batch"),
        (_report(("case-a", "case-a")), "duplicate instance IDs"),
        (_report(("case-a", "case-b"), resolved_ids=("case-a", "case-a")), "duplicate instance IDs"),
        (_report(("case-a", "case-b"), resolved_ids=("case-a",)), "gold did not resolve"),
        (_report(("case-a", "case-b"), submitted_ids=("case-a", "case-x")), "outside the batch"),
    ],
)
def test_gold_control_must_cover_exact_batch_ids(gold_report: str, expected_detail: str, capsys) -> None:
    reports = {
        "gold.cell-gold-20260929.json": gold_report,
        "model.cell-20260929.json": _report(("case-a", "case-b")),
    }
    assert _score_report(reports, batch_name="cell", ids=("case-a", "case-b")) == 1
    assert expected_detail in capsys.readouterr().out


@pytest.mark.parametrize(
    ("model_report", "expected_detail"),
    [
        (_report(("case-a", "case-x")), "outside the batch"),
        (_report(("case-a", "case-b"), submitted_ids=("case-a", "case-x")), "outside the batch"),
        (_report(("case-a",)), "report totals"),
    ],
)
def test_model_report_must_match_the_gold_verified_batch(
    model_report: str, expected_detail: str, capsys
) -> None:
    reports = {
        "gold.cell-gold-20260929.json": _report(("case-a", "case-b")),
        "model.cell-20260929.json": model_report,
    }
    assert _score_report(reports, batch_name="cell", ids=("case-a", "case-b")) == 1
    output = capsys.readouterr().out
    assert expected_detail in output
    assert "a model report does not cover" in output


def test_batch_name_containing_gold_keeps_model_report_ordinary(capsys) -> None:
    reports = {
        "gold.gold-cell-gold-20260929.json": _report(),
        "model.gold-cell-gold-20260929.json": _report(submitted_ids=("case-x",)),
    }
    assert _score_report(reports) == 1
    output = capsys.readouterr().out
    assert output.count("[ok  ]") == 1
    assert "[FAIL] model.gold-cell-gold-20260929.json" in output


def test_model_report_with_missing_outcome_is_not_verified(capsys) -> None:
    ids = ("case-a", "case-b")
    model = json.loads(_report(ids, resolved_ids=("case-a",)))
    model["unresolved_ids"] = []
    reports = {
        "gold.cell-gold-20260929.json": _report(ids),
        "model.cell-20260929.json": json.dumps(model),
    }
    assert _score_report(reports, batch_name="cell", ids=ids) == 1
    assert "report outcomes miss batch instances" in capsys.readouterr().out


def test_older_model_report_can_prove_its_instance_set_from_outcomes(capsys) -> None:
    ids = ("case-a", "case-b")
    model = json.loads(_report(ids, resolved_ids=("case-a",)))
    del model["submitted_ids"]
    reports = {
        "gold.cell-gold-20260928.json": _report(ids),
        "model.cell-20260929.json": json.dumps(model),
    }
    assert _score_report(reports, batch_name="cell", ids=ids) == 0
    assert "[FAIL]" not in capsys.readouterr().out
