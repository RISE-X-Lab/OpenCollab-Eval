"""Tests for workflow prompt, metadata, and patch-path policies."""

from __future__ import annotations

import subprocess

import pytest
from gen_prediction_workflow_support import (
    FIXTURE,
    gpw,
)
from gen_prediction_workflow_support import (
    isolated_solver_snapshot as _isolated_solver_snapshot,  # noqa: F401
)

from opencollab_eval.engine.evaluator import EvalResult, EvalTask


def test_fail_to_pass_ids_parses_json_string():
    assert gpw._fail_to_pass_ids(FIXTURE) == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_fail_to_pass_ids_accepts_list_and_missing():
    assert gpw._fail_to_pass_ids({"FAIL_TO_PASS": ["a", "b"]}) == ["a", "b"]
    assert gpw._fail_to_pass_ids({}) == []


@pytest.mark.parametrize(
    ("max_steps", "budget", "timeout"),
    [
        (0, 100, 5.0),
        (5, 0, 5.0),
        (5, 100, 0),
        (5, 100, float("nan")),
        (5, 100, float("inf")),
    ],
)
def test_workflow_generation_limits_share_single_agent_validation(
    max_steps,
    budget,
    timeout,
):
    with pytest.raises(ValueError):
        gpw.gp.validate_generation_limits(
            max_steps=max_steps,
            budget=budget,
            timeout=timeout,
        )


@pytest.mark.parametrize(
    "checkpoint_interval",
    [-1, float("nan"), float("inf"), True, "bad"],
)
def test_workflow_generation_rejects_invalid_checkpoint_interval(
    checkpoint_interval,
):
    with pytest.raises(ValueError, match="checkpoint-interval"):
        gpw.validate_workflow_limits(
            max_steps=5,
            budget=100,
            timeout=5,
            checkpoint_interval=checkpoint_interval,
        )


def test_workflow_generation_accepts_disabled_checkpointing():
    assert gpw.validate_workflow_limits(
        max_steps=5,
        budget=100,
        timeout=5,
        checkpoint_interval=0,
    ) == (5, 100, 5.0, 0.0)


def test_build_task_lists_target_tests_without_literal_values():
    prompt = gpw.build_task(FIXTURE)
    assert "tests/test_widget.py::test_empty" in prompt
    assert "Widget explodes on empty input." in prompt


def test_build_task_can_omit_hidden_grading_ids_for_blind_validation():
    prompt = gpw.build_task(FIXTURE, include_fail_to_pass=False)

    assert "Blind validation mode" in prompt
    assert "Widget explodes on empty input." in prompt
    assert "tests/test_widget.py::test_empty" not in prompt
    assert "Tests that must pass after your fix" not in prompt


def test_build_extras_populates_hidden_data_by_default():
    extras = gpw.build_extras(FIXTURE)

    assert extras["test_patch"] == FIXTURE["test_patch"]
    assert extras["fail_to_pass"] == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_build_extras_omits_hidden_data_for_blind_validation():
    extras = gpw.build_extras(FIXTURE, include_hidden_tests=False)

    assert extras == {"blind_validation": True}
    assert "test_patch" not in extras
    assert "fail_to_pass" not in extras


def test_validation_council_defaults_to_blind_validation():
    assert gpw._blind_validation_default("validation-council-solve", None) is True
    assert gpw._blind_validation_default("generate_review_fix", None) is False
    assert gpw._blind_validation_default("validation-council-solve", False) is False
    assert gpw._blind_validation_default("generate_review_fix", True) is True


def test_generate_path_resolves_validation_council_blind_default_from_spec():
    class Spec:
        name = "validation-council-solve"

    def workflow_fn():
        return None

    workflow_fn.__workflow_spec__ = Spec()

    assert gpw._resolve_blind_validation(workflow_fn, None) is True
    assert gpw._resolve_blind_validation(workflow_fn, False) is False


def test_validation_artifact_paths_flags_temp_and_test_files():
    patch = "\n".join(
        [
            "diff --git a/widget.py b/widget.py",
            "diff --git a/tests/tmp_probe.py b/tests/tmp_probe.py",
            "diff --git a/pkg/tests/test_widget.py b/pkg/tests/test_widget.py",
            "diff --git a/.opencollab-validation/probe.py b/.opencollab-validation/probe.py",
        ]
    )

    assert gpw._validation_artifact_paths(patch) == [
        "tests/tmp_probe.py",
        "pkg/tests/test_widget.py",
        ".opencollab-validation/probe.py",
    ]


def test_validation_artifact_paths_does_not_flag_production_test_module():
    patch = "\n".join(
        [
            "diff --git a/django/test/testcases.py b/django/test/testcases.py",
            "diff --git a/sklearn/utils/_testing.py b/sklearn/utils/_testing.py",
        ]
    )

    assert gpw._validation_artifact_paths(patch) == []


def test_workflow_result_extracts_allowed_and_disallowed_paths():
    result = {
        "allowed_patch_paths": ["b/pkg/widget.py", " /src/core.py "],
        "disallowed_patch_paths": ["tests/test_widget.py"],
    }

    assert gpw._workflow_allowed_patch_paths(result) == {
        "b/pkg/widget.py",
        "src/core.py",
    }
    assert gpw._workflow_disallowed_patch_paths(result) == {"tests/test_widget.py"}


def test_patch_paths_to_remove_respects_workflow_allowlist():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/pkg/notes.txt b/pkg/notes.txt",
            "diff --git a/tests/test_widget.py b/tests/test_widget.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
        disallowed_paths=set(),
    ) == ["pkg/notes.txt", "tests/test_widget.py"]


def test_patch_paths_to_remove_honors_disallowed_paths():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/tmp/check.py b/tmp/check.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py", "tmp/check.py"},
        disallowed_paths={"tmp/check.py"},
    ) == ["tmp/check.py"]


def test_empty_allowlist_removes_every_patch_path_for_guarded_workflow():
    patch = "diff --git a/pkg/widget.py b/pkg/widget.py"

    assert gpw._patch_paths_to_remove(patch, allowed_paths=set()) == ["pkg/widget.py"]


def test_agent_allowlist_cannot_override_test_artifact_blacklist():
    patch = "diff --git a/src/test_parser.py b/src/test_parser.py"

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/test_parser.py"},
    ) == ["src/test_parser.py"]


@pytest.mark.parametrize(
    "path",
    ["tests/test_bug.py", ".opencollab-validation/probe.py"],
)
def test_agent_allowlist_cannot_admit_blind_validation_artifact(path):
    patch = f"diff --git a/{path} b/{path}"

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={path},
        disallowed_paths=set(),
    ) == [path]


def test_patch_paths_decode_default_git_c_quoted_unicode(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = repo / "src"
    source.mkdir()
    target = source / "café.py"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    target.write_text("new\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert "\\303\\251" in patch
    assert gpw._patch_paths(patch) == ["src/café.py"]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/café.py"},
    ) == []


def test_patch_path_normalization_preserves_repo_relative_b_directory():
    patch = "diff --git a/b/foo.py b/b/foo.py"

    assert gpw._normalize_patch_path("b/foo.py") == "b/foo.py"
    assert gpw._patch_entries(patch) == [("b/foo.py", "b/foo.py")]
    assert gpw._patch_paths(patch) == ["b/foo.py"]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"b/foo.py"},
    ) == []
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"foo.py"},
    ) == ["b/foo.py"]


def test_test_to_allowed_rename_removes_both_endpoints():
    patch = "\n".join(
        [
            "diff --git a/tests/test_widget.py b/pkg/widget.py",
            "similarity index 100%",
            "rename from tests/test_widget.py",
            "rename to pkg/widget.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
    ) == ["tests/test_widget.py", "pkg/widget.py"]


def test_copy_with_unallowed_endpoint_removes_both_endpoints():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/tmp/validation_copy.py",
            "similarity index 100%",
            "copy from pkg/widget.py",
            "copy to tmp/validation_copy.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
    ) == ["pkg/widget.py", "tmp/validation_copy.py"]


def test_git_c_quoted_unicode_rename_removes_both_endpoints(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source_dir = repo / "tests"
    source_dir.mkdir()
    source = source_dir / "naïve.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    target_dir = repo / "src"
    target_dir.mkdir()
    target = target_dir / "café.py"
    source.rename(target)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--find-renames"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert "rename from" in patch
    assert "\\303\\257" in patch
    assert "\\303\\251" in patch
    assert gpw._patch_entries(patch) == [("tests/naïve.py", "src/café.py")]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/café.py"},
    ) == ["tests/naïve.py", "src/café.py"]


def test_json_safe_degrades_unknown_objects():
    class Thing:
        def __str__(self):
            return "thing"

    assert gpw._json_safe({"x": [Thing()]}) == {"x": ["thing"]}


def test_result_metrics_json_safes_workflow_result_without_patch():
    class Thing:
        def __str__(self):
            return "thing"

    result = EvalResult(
        task_id="i",
        patch="diff --git a/a b/a",
        patch_produced=True,
        tokens_used=1,
        steps=2,
        duration=3.0,
        workflow_result={"x": Thing()},
    )

    metrics = gpw._result_metrics(result)

    assert "patch" not in metrics
    assert metrics["workflow_result"] == {"x": "thing"}


def test_evaltask_contract_matches_non_blind_extras():
    # Mirror how generate() builds the EvalTask without needing docker.
    task = EvalTask(
        task_id=FIXTURE["instance_id"],
        description=gpw.build_task(FIXTURE),
        extras=gpw.build_extras(FIXTURE),
    )
    assert task.extras["test_patch"] == FIXTURE["test_patch"]
    assert task.extras["fail_to_pass"] == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]
