"""Batch completion follows verified output after each generator stops."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import run_swebench_smoke_batch as driver
from tests.support.swe_eval_status_support import _strict_modern_prediction


@pytest.mark.parametrize(
    ("returncode", "output_state", "expected"),
    [(124, "completed", 0), (0, "completed", 0), (124, "missing", 1), (0, "missing", 1), (124, "invalid", 1)],
)
def test_smoke_batch_exit_follows_completed_output(
    monkeypatch, tmp_path, capsys, returncode, output_state, expected,
):
    instances = tmp_path / "instances"
    instances.mkdir()
    for number in (1, 2):
        (instances / f"task-{number}.json").write_text(json.dumps({"instance_id": f"task-{number}"}))
    output = tmp_path / "output/predictions.jsonl"
    monkeypatch.setattr(driver, "make_test_spec", lambda *a, **kw: SimpleNamespace(instance_image_key="fixture-image"))
    original_run = driver._run_generator
    visited = []

    def generate(command, **kwargs):
        instance_file = command[command.index("--instance-file") + 1]
        instance_id = json.loads(Path(instance_file).read_text())["instance_id"]
        visited.append(instance_id)
        first = instance_id == "task-1"
        code = returncode if first else 0
        state = output_state if first else "completed"
        prediction = _strict_modern_prediction(
            status="done_with_timeout_patch" if code == 124 else "done", returncode=code,
        )
        prediction["instance_id"] = instance_id
        prediction["workflow_metric"]["instance_id"] = instance_id
        if state == "invalid":
            prediction["workflow_metric"]["submission_eligible"] = False
        child = (
            "import json, pathlib, sys\n"
            "target = pathlib.Path(sys.argv[1])\n"
            "if sys.argv[2] != 'missing':\n"
            "    with target.open('a') as handle:\n"
            "        handle.write(sys.argv[3] + '\\n')\n"
            "sys.exit(int(sys.argv[4]))\n"
        )
        return original_run(
            [sys.executable, "-c", child, str(output), state, json.dumps(prediction), str(code)], **kwargs,
        )

    monkeypatch.setattr(driver, "_run_generator", generate)
    monkeypatch.setattr(sys, "argv", [
        "smoke-batch", "--instances-dir", str(instances), "--output-dir", str(output.parent),
        "--model-name", "fixture-model",
    ])
    assert driver.main() == expected
    assert visited == ["task-1", "task-2"]
    if returncode:
        assert f"exit code {returncode}" in capsys.readouterr().out
