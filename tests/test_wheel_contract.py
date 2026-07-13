from __future__ import annotations

import os
from importlib.metadata import version as distribution_version
from importlib.resources import files
from pathlib import Path

import opencollab.sdk
from opencollab.sdk import SDK_API_VERSION

import opencollab_eval


def test_opencollab_sdk_can_come_from_the_built_wheel() -> None:
    expected_root = os.environ.get("OPENCOLLAB_EXPECTED_WHEEL_ROOT")
    if expected_root:
        assert Path(opencollab.sdk.__file__).is_relative_to(Path(expected_root))
    expected_eval_root = os.environ.get("OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT")
    if expected_eval_root:
        assert Path(opencollab_eval.__file__).is_relative_to(Path(expected_eval_root))
    sdk_version = tuple(int(part) for part in distribution_version("opencollab").split("."))
    assert (0, 2, 1) <= sdk_version < (0, 3)
    assert SDK_API_VERSION == 1
    assert "roles:" in files("opencollab_eval.configs").joinpath("team.swebench.yaml").read_text(encoding="utf-8")
