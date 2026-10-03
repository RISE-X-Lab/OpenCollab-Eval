"""Merged prediction summaries count the attempt actually adopted by the CLI."""

import json
import sys

import pytest

from experiment.analysis import merged_preds
from opencollab_eval.commands import batch as batch_cli
from tests.experiment import batch_support as launcher
from tests.experiment.test_cell_report_attempt_seats import _identified_attempt

_oc_repo = pytest.fixture(name="oc_repo")(launcher.oc_repo.__wrapped__)
_experiment = pytest.fixture(name="experiment")(launcher.experiment.__wrapped__)


@pytest.mark.parametrize("status,later_count,selected", [
    ("failed", 0, "original"),
    ("completed", 1, "retry"),
])
def test_merged_cli_counts_actual_adopted_attempt(experiment, tmp_path, monkeypatch, capsys,
                                                status, later_count, selected):
    original_spec = experiment["spec"]
    text = original_spec.read_text().replace("rows: {start: 1, stop: 2}", "rows: {start: 1, stop: 1}")
    original_spec.write_text(text)
    retry_spec = original_spec.with_name("retry.yaml")
    retry_spec.write_text(text.replace("name: t1", "name: retry\nretry_of: t1"))
    cli_args = ["--experiment-dir", str(experiment["dir"])]
    for spec in [original_spec, retry_spec]:
        assert batch_cli.main([*cli_args, "plan", str(spec)], remote_factory=lambda host: None) == 0
    for name, record_id, outcome in [("t1", "original", "completed"), ("retry", "retry", status)]:
        prediction, metric = _identified_attempt(record_id, 10, instance_id="a__a-1", status=outcome)
        if outcome == "failed":
            metric["run_summary"]["reason"] = "APIError: Upstream request failed"
        directory = tmp_path / "batches" / name
        directory.mkdir(exist_ok=True)
        (directory / "metrics.jsonl").write_text(json.dumps(metric) + "\n")
        (directory / "preds-team.jsonl").write_text(json.dumps(prediction) + "\n")
    output = tmp_path / "merged.jsonl"
    monkeypatch.setattr(sys, "argv", ["merged-preds", str(original_spec), "--experiment-dir",
                                     str(experiment["dir"]), "--out", str(output)])
    capsys.readouterr()
    assert merged_preds.main() == 0
    assert json.loads(output.read_text())["record_id"] == selected
    stdout = capsys.readouterr().out
    assert f"{later_count} taken from a later attempt" in stdout
    assert "attempt 1/2" in stdout if selected == "original" else "attempt 2/2" in stdout
