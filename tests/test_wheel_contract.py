from __future__ import annotations

import hashlib
import os
from importlib.metadata import files as distribution_files
from importlib.metadata import version as distribution_version
from pathlib import Path

import opencollab
import opencollab.builtin_workflows
import opencollab.environments
import opencollab.patches
import opencollab.profiles
import opencollab.tools
import opencollab.workflows
from packaging.version import Version

import opencollab_eval
from opencollab_eval.workflow_loader import load_workflow


def test_opencollab_sdk_can_come_from_the_built_wheel() -> None:
    expected_root = os.environ.get("OPENCOLLAB_EXPECTED_WHEEL_ROOT")
    if expected_root:
        assert Path(opencollab.__file__).is_relative_to(Path(expected_root))
    expected_eval_root = os.environ.get("OPENCOLLAB_EVAL_EXPECTED_WHEEL_ROOT")
    if expected_eval_root:
        assert Path(opencollab_eval.__file__).is_relative_to(Path(expected_eval_root))
    sdk_version = Version(distribution_version("opencollab")).release
    assert (0, 8, 0) <= sdk_version < (0, 9)
    assert callable(opencollab.builtin_workflows.duo)
    assert [spec.name for spec in opencollab.builtin_workflows.get_builtin_workflows().list_specs()] == ["duo"]
    assert opencollab.profiles.resolve_profile_name("base") == "single2"
    assert callable(opencollab.patches.patch_paths)
    assert callable(opencollab.tools.evidence_tools)
    assert callable(opencollab.tools.builtin_tools)
    assert callable(opencollab.workflows.workflow)
    assert callable(opencollab.environments.attach_container)
    assert callable(load_workflow)


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
