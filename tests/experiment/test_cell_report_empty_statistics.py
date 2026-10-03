"""A cell without valid samples writes standard missing JSON values."""

import json
from pathlib import Path

import pytest

from opencollab_eval.commands import batch as batch_cli
from opencollab_eval.experiment import cell_report
from tests.experiment import batch_support as launcher

_oc_repo = pytest.fixture(name="oc_repo")(launcher.oc_repo.__wrapped__)
_experiment = pytest.fixture(name="experiment")(launcher.experiment.__wrapped__)


def _reject_constant(value):
    raise ValueError(f"nonstandard JSON numeric literal {value}")


@pytest.mark.xfail(strict=True, reason="P2-20 undefined intervals are serialized as NaN")
def test_report_without_valid_samples_writes_null_intervals(experiment, tmp_path):
    args = ["--experiment-dir", str(experiment["dir"])]
    assert batch_cli.main([*args, "plan", str(experiment["spec"])], remote_factory=lambda host: None) == 0
    data = tmp_path / "batches/t1"
    data.mkdir(exist_ok=True)
    (data / "metrics.jsonl").write_text("".join(json.dumps({
        "instance_id": iid, "run_summary": {"status": "failed", "reason": "APIError: Upstream request failed"},
    }) + "\n" for iid in ["a__a-1", "b__b-2"]))
    output = tmp_path / "report.json"
    assert batch_cli.main([*args, "report", str(experiment["spec"]), "--scanner", "none", "--json", str(output)],
                          remote_factory=lambda host: None) == 0
    document = json.loads(output.read_text(), parse_constant=_reject_constant)
    summary = document["summary"]
    assert summary["valid"] == 0
    assert summary["delivered"] == 0
    assert summary["alpha"] is None
    assert summary["ci95"] == [None, None]
    assert summary["every_delegate_rate"] is None
    assert summary["every_delegate_ci95"] == [None, None]
    assert cell_report.clopper_pearson(0, 0) == (None, None)
