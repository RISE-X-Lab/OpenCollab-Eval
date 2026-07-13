"""Shared fixtures for workflow prediction generation tests."""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest
from package_test_support import module_path

_SWEBENCH_DIR = module_path("opencollab_eval.generation.gen_prediction").parent
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gpw = pytest.importorskip("opencollab_eval.generation.gen_prediction_workflow")


FIXTURE = {
    "instance_id": "acme__widget-42",
    "base_commit": "a" * 40,
    "repo": "acme/widget",
    "problem_statement": "Widget explodes on empty input.",
    "hints_text": "look at parse()",
    "FAIL_TO_PASS": '["tests/test_widget.py::test_empty", "tests/test_widget.py::test_none"]',
    "test_patch": (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n"
        "@@ -1 +1,2 @@\n x=1\n+assert widget('') == ''\n"
    ),
}


@pytest.fixture(autouse=True)
def isolated_solver_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = SimpleNamespace(
        as_dict=lambda: {
            "enabled": True,
            "anonymous_head": "b" * 40,
            "base_tree": "c" * 40,
            "commit_count": 1,
            "remote_count": 0,
            "extra_git_metadata": 0,
            "removed_git_metadata": 0,
        }
    )
    monkeypatch.setattr(
        gpw.gp,
        "prepare_solver_git_snapshot",
        lambda container_id, expected_base_commit: evidence,
    )
    baseline = SimpleNamespace(snapshot=evidence, cleanup=lambda: None)
    monkeypatch.setattr(
        gpw.gp,
        "prepare_trusted_patch_baseline",
        lambda container_id, snapshot: baseline,
    )
    monkeypatch.setattr(gpw.gp, "anonymous_solver_task_id", lambda: "solver-opaque-test-id")
    monkeypatch.setattr(gpw, "require_container_quiescence", lambda container_id: None)


def trusted_proof(patch: str) -> dict:
    """Build valid trusted-extraction evidence for a synthetic patch."""
    encoded = patch.encode("utf-8")
    return gpw.gp.gen_prediction_patch.TrustedPatchExtraction(
        fixed_anonymous_base="b" * 40,
        base_tree="c" * 40,
        baseline_archive_sha256="d" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="e" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    ).as_dict()
