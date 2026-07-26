from __future__ import annotations

import hashlib
import os
from importlib.metadata import files as distribution_files
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


def test_eval_wheel_contains_the_published_license_files() -> None:
    source_root = Path(
        os.environ.get(
            "OPENCOLLAB_EVAL_SOURCE_ROOT",
            Path(__file__).resolve().parents[1],
        )
    ).resolve()
    names = {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"}
    expected_root = os.environ.get("OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT")
    if not expected_root:
        pyproject = (source_root / "pyproject.toml").read_text(encoding="utf-8")
        assert 'license-files = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]' in pyproject
        return

    entries = distribution_files("opencollab-eval")
    assert entries is not None
    installed = {
        entry.name: entry.locate()
        for entry in entries
        if entry.name in names
    }
    assert installed.keys() == names
    for name, path in installed.items():
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(
            (source_root / name).read_bytes()
        ).digest()
