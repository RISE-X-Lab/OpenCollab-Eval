"""Concrete regressions at the integrated research reporting boundaries."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from opencollab_eval.experiment import cell_report
from tests.support.paths import SOURCE_ROOT

ROOT = SOURCE_ROOT


def test_data_module_import_does_not_require_runtime_observers() -> None:
    code = (
        "import sys; import opencollab_eval.experiment.task_sampling; "
        "assert 'opencollab_eval.experiment.arm_audit' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), check=True)


def test_retry_consumption_retains_selected_and_recorded_totals() -> None:
    original = cell_report.RunRow("task", "failed", "APIError", 100, 1)
    retry = cell_report.RunRow("task", "completed", "", 200, 2)
    rows = cell_report.merge_attempts([("first", [original]), ("retry", [retry])])
    summary = cell_report.summarize(rows, team=False)
    assert summary["tokens_total"] == 200
    assert summary["tokens_all_attempts"] == 300
    document = cell_report.report_document(
        rows, summary, [], [(cell_report.RunRow("other", "completed", "", 50, 1), "replaced")]
    )
    assert document["recorded_tokens_including_excluded"] == 350


def _merged_preds_module():
    spec = importlib.util.spec_from_file_location("research_merged_preds", ROOT / "experiment/analysis/merged_preds.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merged_prediction_selects_the_observed_record_within_one_batch() -> None:
    module = _merged_preds_module()
    row = cell_report.RunRow("task", "completed", "", 1, 1, record_id="valid")
    assert (
        module._selected_prediction(
            row, [({"record_id": "valid"}, "valid-json"), ({"record_id": "new-invalid"}, "invalid-json")]
        )
        == "valid-json"
    )


def test_batch_input_write_preserves_a_symlink_target(tmp_path: Path) -> None:
    from types import SimpleNamespace

    import pytest

    from opencollab_eval.commands.batch import Batch

    target = tmp_path / "unrelated.json"
    target.write_text("preserved")
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "instances.json").symlink_to(target)
    batch = SimpleNamespace(
        local_dir=launch, spec=SimpleNamespace(instances_file="instances.json"), instances_text="replacement"
    )
    with pytest.raises(OSError):
        Batch.write_inputs(batch)
    assert target.read_text() == "preserved"


def test_batch_record_publication_preserves_a_symlink_target(tmp_path: Path) -> None:
    from types import SimpleNamespace

    import pytest

    from opencollab_eval.commands.batch import Batch

    target = tmp_path / "unrelated.json"
    target.write_text("preserved")
    path = tmp_path / "batch.json"
    path.symlink_to(target)
    batch = SimpleNamespace(previous_record=lambda: None, record_path=lambda: path)
    with pytest.raises(OSError):
        Batch.save_record(batch, {"spec": {"name": "new"}})
    assert target.read_text() == "preserved"
