from __future__ import annotations

import sys
from pathlib import Path

import pytest

import opencollab_eval.generation.gen_prediction_openhands as gpo


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_openhands_main_rejects_invalid_timeout_before_container_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: float,
) -> None:
    monkeypatch.setattr(gpo.gp, "start_container_with_marker", pytest.fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction_openhands.py",
            "--instance-file",
            str(tmp_path / "missing-instance.json"),
            "--output",
            str(tmp_path / "predictions.jsonl"),
            "--command",
            "openhands --headless",
            "--timeout",
            str(value).lower() if isinstance(value, bool) else str(value),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        gpo.main()

    assert exc_info.value.code == 2
