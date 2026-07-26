"""Tests for workflow prompt, metadata, and patch-path policies."""

from __future__ import annotations

import copy
import hashlib
import subprocess

import pytest
from gen_prediction_workflow_support import (
    FIXTURE,
    gpw,
)
from gen_prediction_workflow_support import (
    isolated_solver_snapshot as _isolated_solver_snapshot,  # noqa: F401
)
from gen_prediction_workflow_support import trusted_proof as _trusted_proof

from opencollab_eval.engine.evaluator import EvalResult, EvalTask
from opencollab_eval.engine.swe_generation_proof import current_generation_proof_valid
from opencollab_eval.generation import gen_prediction_patch as patcher
from opencollab_eval.patch_diff import patch_entries, remove_generated_artifact_blocks


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


def test_bundled_workflows_use_public_hyphenated_names():
    registry = gpw._BUNDLED_WORKFLOWS

    assert registry["validation-council-solve"].__workflow_spec__.name == (
        "validation-council-solve"
    )
    assert registry["base-team"].__workflow_spec__.name == "base-team"
    assert registry["team-pro"].__workflow_spec__.name == "team-pro"
    assert "validation_council_solve" not in registry


def test_generate_path_resolves_validation_council_blind_default_from_spec():
    class Spec:
        name = "validation-council-solve"

    def workflow_fn():
        return None

    workflow_fn.__workflow_spec__ = Spec()

    assert gpw._resolve_blind_validation(workflow_fn, None) is True
    assert gpw._resolve_blind_validation(workflow_fn, False) is False


def test_patch_path_audit_records_every_candidate_path_without_path_policy():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/pkg/notes.txt b/pkg/notes.txt",
            "diff --git a/tests/test_widget.py b/tests/test_widget.py",
        ]
    )

    assert gpw._patch_path_audit(patch) == {
        "actual_paths": ["pkg/notes.txt", "pkg/widget.py", "tests/test_widget.py"],
        "selection_policy": "all_changes_against_verified_baseline",
    }


def test_patch_path_audit_keeps_test_and_temporary_named_sources():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/tmp/check.py b/tmp/check.py",
        ]
    )

    assert gpw._patch_path_audit(patch)["actual_paths"] == ["pkg/widget.py", "tmp/check.py"]


@pytest.mark.parametrize(
    "artifact_path",
    [
        "pkg/__pycache__/widget.cpython-311.pyc",
        "pkg/__pycache__/widget.pyo",
        "build/legacy.pyc",
        "tests/__pycache__/test_widget.cpython-311-pytest-8.4.1.pyc",
        "pkg/.pytest_cache/v/cache/nodeids",
        ".hypothesis/constants/widget",
        ".yarn/install-state.gz",
    ],
)
def test_guarded_extraction_removes_generated_runtime_artifacts(
    monkeypatch,
    artifact_path,
):
    source_patch = (
        "diff --git a/pkg/widget.py b/pkg/widget.py\n"
        "--- a/pkg/widget.py\n"
        "+++ b/pkg/widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    patch = source_patch + (
        f"diff --git a/{artifact_path} b/{artifact_path}\n"
        "new file mode 100644\n"
        f"Binary files /dev/null and b/{artifact_path} differ\n"
    )

    class Extraction:
        @staticmethod
        def as_dict():
            return _trusted_proof(patch)

    monkeypatch.setattr(
        patcher,
        "extract_patch_trusted",
        lambda *_args: (patch, Extraction()),
    )
    monkeypatch.setattr(
        patcher,
        "project_candidate_patch",
        lambda **kwargs: (
            "a" * 40 if kwargs["patch"] == patch else "b" * 40,
            kwargs["patch"],
            ("pkg/widget.py",),
            (("pkg/widget.py", "100644", "100644"),),
        ),
    )

    class Baseline:
        git_dir = object()

        class snapshot:
            anonymous_head = "b" * 40

    filtered, removed, proof = gpw.extract_patch_guarded(
        "container",
        Baseline(),
    )

    encoded = source_patch.encode("utf-8")
    assert filtered == source_patch
    assert removed == [artifact_path]
    assert proof["patch_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert proof["patch_bytes"] == len(encoded)
    assert proof["pre_sanitization_candidate_tree"] == "a" * 40
    assert proof["candidate_tree"] == "b" * 40
    assert gpw._patch_path_audit(filtered) == {
        "actual_paths": ["pkg/widget.py"],
        "selection_policy": "all_changes_against_verified_baseline",
    }
    assert proof["workspace_integrity"]["outcome"] == "sanitize_then_continue"
    metric = {
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": gpw.gp.prepare_solver_git_snapshot("cid", "base").as_dict(),
        "trusted_patch_extraction": proof,
    }
    assert current_generation_proof_valid(metric, filtered) is True
    for field in (
        "pre_sanitization_patch_sha256",
        "pre_sanitization_candidate_tree",
        "candidate_tree",
    ):
        incomplete = copy.deepcopy(metric)
        del incomplete["trusted_patch_extraction"][field]
        assert current_generation_proof_valid(incomplete, filtered) is False
    same_sha = copy.deepcopy(metric)
    same_sha["trusted_patch_extraction"]["pre_sanitization_patch_sha256"] = proof[
        "patch_sha256"
    ]
    assert current_generation_proof_valid(same_sha, filtered) is False
    same_tree = copy.deepcopy(metric)
    same_tree["trusted_patch_extraction"]["pre_sanitization_candidate_tree"] = proof[
        "candidate_tree"
    ]
    assert current_generation_proof_valid(same_tree, filtered) is False


def test_guarded_extraction_keeps_source_omitted_from_verifier_allowlist(monkeypatch):
    patch = (
        "diff --git a/pkg/widget.py b/pkg/widget.py\n"
        "--- a/pkg/widget.py\n"
        "+++ b/pkg/widget.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/docs/widget.md b/docs/widget.md\n"
        "--- a/docs/widget.md\n"
        "+++ b/docs/widget.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    class Extraction:
        @staticmethod
        def as_dict():
            return {
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                "patch_bytes": len(patch),
            }

    monkeypatch.setattr(
        patcher,
        "extract_patch_trusted",
        lambda *_args: (patch, Extraction()),
    )

    extracted, removed, proof = gpw.extract_patch_guarded(
        "container",
        object(),
    )

    assert extracted == patch
    assert removed == []
    assert gpw._patch_path_audit(extracted)["actual_paths"] == [
        "docs/widget.md",
        "pkg/widget.py",
    ]


def test_guarded_extraction_keeps_strict_proof_schema_and_separate_audit(monkeypatch):
    patch = "diff --git a/pkg/widget.py b/pkg/widget.py\n+fixed\n"

    class Extraction:
        @staticmethod
        def as_dict():
            return _trusted_proof(patch)

    monkeypatch.setattr(
        patcher,
        "extract_patch_trusted",
        lambda *_args: (patch, Extraction()),
    )
    extracted, _removed, proof = gpw.extract_patch_guarded(
        "container",
        object(),
    )
    metric = {
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": gpw.gp.prepare_solver_git_snapshot("cid", "base").as_dict(),
        "trusted_patch_extraction": proof,
        "patch_path_audit": gpw._patch_path_audit(extracted),
    }

    assert "path_audit" not in proof
    assert current_generation_proof_valid(metric, extracted) is True
    assert metric["patch_path_audit"]["actual_paths"] == ["pkg/widget.py"]


def test_guarded_extraction_keeps_model_test_source(monkeypatch):
    patch = (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n"
        "+++ b/tests/test_widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    class Extraction:
        @staticmethod
        def as_dict():
            return {"patch_sha256": "0" * 64, "patch_bytes": len(patch)}

    monkeypatch.setattr(
        patcher,
        "extract_patch_trusted",
        lambda *_args: (patch, Extraction()),
    )

    extracted, removed, _proof = gpw.extract_patch_guarded("container", object())

    assert extracted == patch
    assert removed == []


def test_generated_artifact_filter_rejects_mixed_path_rename():
    bytecode_path = "tests/__pycache__/test_widget.cpython-311.pyc"
    patch = (
        f"diff --git a/{bytecode_path} b/pkg/widget.py\n"
        "similarity index 100%\n"
        f"rename from {bytecode_path}\n"
        "rename to pkg/widget.py\n"
    )

    with pytest.raises(RuntimeError, match="mixed-path entry"):
        remove_generated_artifact_blocks(patch, {bytecode_path})


def test_generated_artifact_filter_rejects_unmatched_path():
    patch = "diff --git a/pkg/widget.py b/pkg/widget.py\n"

    with pytest.raises(RuntimeError, match="filtering was incomplete"):
        remove_generated_artifact_blocks(
            patch,
            {"tests/__pycache__/test_widget.cpython-311.pyc"},
        )


@pytest.mark.parametrize(
    "artifact_path",
    [
        "pkg/__pycache__/widget.cpython-311.pyc",
        "pkg/__pycache__/widget.pyo",
        "build/legacy.pyc",
        "pkg/.pytest_cache/v/cache/nodeids",
        ".hypothesis/constants/widget",
        ".yarn/install-state.gz",
    ],
)
@pytest.mark.parametrize("change", ["modified", "deleted"])
def test_guarded_artifact_filter_keeps_tracked_changes(artifact_path, change):
    if change == "deleted":
        patch = (
            f"diff --git a/{artifact_path} b/{artifact_path}\n"
            "deleted file mode 100644\n"
            f"--- a/{artifact_path}\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-tracked\n"
        )
    else:
        patch = (
            f"diff --git a/{artifact_path} b/{artifact_path}\n"
            f"--- a/{artifact_path}\n"
            f"+++ b/{artifact_path}\n"
            "@@ -1 +1 @@\n"
            "-tracked\n"
            "+changed\n"
        )

    paths = patcher._new_generated_artifact_paths(patch)
    filtered, removed = remove_generated_artifact_blocks(patch, paths)

    assert filtered == patch
    assert removed == []


def test_candidate_path_audit_has_no_path_allowlist():
    patch = "diff --git a/pkg/widget.py b/pkg/widget.py"

    assert gpw._patch_path_audit(patch) == {
        "actual_paths": ["pkg/widget.py"],
        "selection_policy": "all_changes_against_verified_baseline",
    }


@pytest.mark.parametrize(
    "path",
    ["src/test_parser.py", "tests/test_bug.py", ".opencollab-validation/probe.py"],
)
def test_path_shape_never_removes_a_candidate_change(path):
    patch = f"diff --git a/{path} b/{path}"

    assert gpw._patch_path_audit(patch)["actual_paths"] == [path]


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


def test_patch_path_normalization_preserves_repo_relative_b_directory():
    patch = "diff --git a/b/foo.py b/b/foo.py"

    assert patch_entries(patch) == [("b/foo.py", "b/foo.py")]
    assert gpw._patch_paths(patch) == ["b/foo.py"]
    assert gpw._patch_path_audit(patch)["actual_paths"] == ["b/foo.py"]


def test_test_to_source_rename_keeps_both_endpoints():
    patch = "\n".join(
        [
            "diff --git a/tests/test_widget.py b/pkg/widget.py",
            "similarity index 100%",
            "rename from tests/test_widget.py",
            "rename to pkg/widget.py",
        ]
    )

    assert gpw._patch_path_audit(patch)["actual_paths"] == [
        "pkg/widget.py",
        "tests/test_widget.py",
    ]


def test_copy_records_both_candidate_endpoints():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/tmp/validation_copy.py",
            "similarity index 100%",
            "copy from pkg/widget.py",
            "copy to tmp/validation_copy.py",
        ]
    )

    assert gpw._patch_path_audit(patch)["actual_paths"] == [
        "pkg/widget.py",
        "tmp/validation_copy.py",
    ]


def test_git_c_quoted_unicode_rename_records_both_endpoints(tmp_path):
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
    assert patch_entries(patch) == [("tests/naïve.py", "src/café.py")]
    assert gpw._patch_path_audit(patch)["actual_paths"] == [
        "src/café.py",
        "tests/naïve.py",
    ]


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
