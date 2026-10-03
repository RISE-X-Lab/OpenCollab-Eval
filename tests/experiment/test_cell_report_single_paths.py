"""Single-agent snapshots follow directory and trajectory-file metrics paths."""

from pathlib import Path

import pytest

from opencollab_eval.experiment import cell_report
from tests.experiment.cell_report_support import _seat_file, _write_metrics


@pytest.mark.parametrize("location", ["local", "pulled"])
@pytest.mark.parametrize("shape", ["directory", "file"])
def test_single_snapshot_paths(tmp_path: Path, location: str, shape: str):
    cell = tmp_path / "cell"
    directory = cell / "agent-first"
    _seat_file(directory, "agent.json", aid=-1, role="swe_agent", tokens=25, assistant=3)
    _seat_file(cell / "agent-unrelated", "agent.json", aid=-1, role="swe_agent", tokens=999, assistant=20)
    recorded = directory if location == "local" else Path("/home/example/run") / directory.name
    if shape == "file":
        (directory / "trajectory.jsonl").write_text("")
        recorded /= "trajectory.jsonl"
    _write_metrics(cell, [{
        "instance_id": "case-a", "trajectory_path": str(recorded),
        "run_summary": {"status": "completed", "tokens": 25},
    }])
    rows = cell_report.run_rows(cell, "single")
    assert rows[0].seat_snapshot_found is True
    assert list(rows[0].seats) == ["-1"]
    assert rows[0].seats["-1"].tokens == 25
    assert rows[0].seats["-1"].assistant == 3
