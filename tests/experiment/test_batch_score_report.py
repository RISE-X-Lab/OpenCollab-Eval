"""Identify batch scoring controls from the harness's model and run ID."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from opencollab_eval.commands.batch_scoring import cmd_score_report


def _report(*, resolved: bool = True) -> str:
    return json.dumps(
        {
            "total_instances": 1,
            "submitted_instances": 1,
            "completed_instances": 1,
            "resolved_ids": ["case-a"] if resolved else [],
            "unresolved_ids": [] if resolved else ["case-a"],
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


def _score_report(reports: dict[str, str], *, batch_name: str = "gold-cell") -> int:
    batch = SimpleNamespace(
        spec=SimpleNamespace(name=batch_name),
        host=SimpleNamespace(workdir="/tmp/scoring-workdir"),
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
        "gold.gold-cell-gold-20260929.json": _report(resolved=False),
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
        "gold.gold-cell-gold-20260928.json": _report(resolved=older_resolved),
        "gold.gold-cell-gold-20260929.json": _report(resolved=newer_resolved),
    }
    assert _score_report(reports) == expected


def test_no_gold_control_in_ordinary_batch(capsys) -> None:
    assert _score_report({"model.cell-20260929.json": _report()}, batch_name="cell") == 1
    assert "no gold control" in capsys.readouterr().out
