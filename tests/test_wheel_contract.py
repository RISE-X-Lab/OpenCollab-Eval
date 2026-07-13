from __future__ import annotations

import os
from importlib.metadata import version as distribution_version
from importlib.resources import files
from pathlib import Path

import opencollab.sdk.models as sdk_models
from opencollab.sdk.models import SDK_API_VERSION

import opencollab_eval


def test_opencollab_sdk_can_come_from_the_built_wheel() -> None:
    expected_root = os.environ.get("OPENCOLLAB_EXPECTED_WHEEL_ROOT")
    if expected_root:
        assert Path(sdk_models.__file__).is_relative_to(Path(expected_root))
    expected_eval_root = os.environ.get("OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT")
    if expected_eval_root:
        assert Path(opencollab_eval.__file__).is_relative_to(Path(expected_eval_root))
    sdk_version = tuple(int(part) for part in distribution_version("opencollab").split("."))
    assert (0, 3) <= sdk_version < (0, 4)
    assert SDK_API_VERSION == 2
    configs = files("opencollab_eval.configs")
    for filename in ("team.swebench.yaml", "team.self.collab.yaml"):
        assert "roles:" in configs.joinpath(filename).read_text(encoding="utf-8")
