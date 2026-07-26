from __future__ import annotations

import os
from importlib.metadata import version as distribution_version
from importlib.resources import files
from pathlib import Path

import opencollab
import opencollab.environments
import opencollab.tools
import opencollab.workflows

import opencollab_eval
from opencollab_eval.commands.swe_v1_prolite_config import verify_runtime_import_contract


def test_opencollab_sdk_can_come_from_the_built_wheel() -> None:
    expected_root = os.environ.get("OPENCOLLAB_EXPECTED_WHEEL_ROOT")
    if expected_root:
        assert Path(opencollab.__file__).is_relative_to(Path(expected_root))
    expected_eval_root = os.environ.get("OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT")
    if expected_eval_root:
        assert Path(opencollab_eval.__file__).is_relative_to(Path(expected_eval_root))
    sdk_version = tuple(int(part) for part in distribution_version("opencollab").split("."))
    assert (0, 4) <= sdk_version < (0, 5)
    assert callable(opencollab.tools.builtin_tools)
    assert callable(opencollab.workflows.workflow)
    assert callable(opencollab.environments.attach_container)
    configs = files("opencollab_eval.configs")
    for filename in ("team.swebench.yaml", "team.self.collab.yaml"):
        assert "roles:" in configs.joinpath(filename).read_text(encoding="utf-8")


def test_installed_wheels_satisfy_the_runtime_import_contract() -> None:
    verify_runtime_import_contract()
