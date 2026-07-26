"""Patch filtering and candidate identity evidence tests."""

from __future__ import annotations

import pytest
from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    _test_only_patch,
    _write_jsonl,
)


def test_remote_runner_filters_test_only_candidate_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    assert namespace["filter_model_patch_for_eval"](patch) == ""
    assert namespace["model_patch_filter_evidence"](prediction) == {
        "source_patch_sha256": patch_sha,
        "eval_patch_sha256": namespace["patch_sha"](""),
        "filtered_patch_paths": ["tests/test_widget.py"],
    }


@pytest.mark.parametrize(
    "path",
    (
        "tests/test_widget.py",
        "tests/helpers.py",
        "testing/helpers.py",
        "spec/helper.js",
    ),
)
def test_eval_selection_filters_candidate_evaluation_surface(tmp_path, path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+def pytest_configure(): pass\n"
    )
    prediction = {"model_patch": patch}
    row = {
        "instance_id": "task-1",
        "FAIL_TO_PASS": ["tests/test_widget.py::test_widget"],
        "test_patch": (
            "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
            "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n"
        ),
    }

    selection = namespace["prepare_eval_patch_selection"](row, prediction, {})

    assert selection["ok"] is True
    assert selection["status"] == "ready"
    assert selection["filtered_patch_paths"] == [path]
    assert selection["model_patch"] == ""


def test_eval_selection_keeps_source_fix_and_filters_candidate_test(tmp_path):
    namespace = _remote_namespace(tmp_path)
    source_block = (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n+++ b/src/widget.py\n@@ -1 +1 @@\n-old\n+fixed\n"
    )
    test_block = (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n"
        "@@ -1 +1 @@\n-old assertion\n+new assertion\n"
    )
    source_patch = source_block + test_block

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1", "FAIL_TO_PASS": ["tests/test_widget.py::test_widget"]},
        {"model_patch": source_patch},
        {},
    )

    assert selection["ok"] is True
    assert selection["model_patch"] == source_block
    assert selection["filtered_patch_paths"] == ["tests/test_widget.py"]
    assert selection["source_patch_sha256"] == namespace["patch_sha"](source_patch)
    assert selection["eval_patch_sha256"] == namespace["patch_sha"](source_block)
    assert selection["source_patch_sha256"] != selection["eval_patch_sha256"]


def test_eval_selection_rejects_candidate_pytest_control_file(tmp_path):
    namespace = _remote_namespace(tmp_path)
    path = "conftest.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+fake pass\n"
    )

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1", "FAIL_TO_PASS": ["tests/test_widget.py::test_widget"]},
        {"model_patch": patch},
        {},
    )

    assert selection["ok"] is False
    assert selection["status"] == "candidate_evaluation_surface_tampering"
    assert selection["tampered_paths"] == [path]


def test_eval_selection_rejects_a_plan_derived_runtime_runner_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    path = "node_modules/.bin/jest"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+fake pass\n"
    )

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1"},
        {"model_patch": patch},
        {},
        [
            {
                "root": "node_modules",
                "required_paths": [path],
                "kind": "directory",
                "candidate_protected": True,
            }
        ],
    )

    assert selection["ok"] is False
    assert selection["status"] == "candidate_evaluation_surface_tampering"
    assert selection["tampered_paths"] == [path]


def test_eval_selection_allows_a_candidate_runtime_manifest_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    path = "package.json"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n"
        '-{"scripts":{"test":"mocha"}}\n'
        '+{"scripts":{"test":"mocha --check-leaks"}}\n'
    )

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1"},
        {"model_patch": patch},
        {},
        [
            {
                "root": "package.json",
                "required_paths": ["package.json"],
                "kind": "file",
                "candidate_protected": False,
            }
        ],
    )

    assert selection["ok"] is True
    assert selection["status"] == "ready"
    assert selection["model_patch"] == patch


def test_test_only_candidate_becomes_a_proven_empty_eval_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": "task-1",
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    metric = {
        "instance_id": "task-1",
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
    }

    result = namespace["_generation_patch_result"](
        {
            "instance_id": "task-1",
            "FAIL_TO_PASS": ["tests/test_widget.py::test_widget"],
        },
        "task-1",
        prediction,
        metric,
        "record_id",
    )

    assert result["status"] == "empty_patch"
    assert result["submission_integrity"] == "filtered_empty_patch_proven"
    assert result["filtered_patch_paths"] == ["tests/test_widget.py"]


def test_filter_model_patch_handles_diff_paths_with_spaces(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/src/app code.py b/src/app code.py\n"
        "--- a/src/app code.py\n"
        "+++ b/src/app code.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/tests/test app.py b/tests/test app.py\n"
        "--- a/tests/test app.py\n"
        "+++ b/tests/test app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered, evidence = namespace["filter_model_patch_with_evidence"](patch)

    assert "src/app code.py" in filtered
    assert "tests/test app.py" not in filtered
    assert evidence == ["tests/test app.py"]


@pytest.mark.parametrize(
    ("patch", "targets"),
    (
        ((
            "diff --git a/test app.py b/test app.py\n"
            "--- a/test app.py\t\n"
            "+++ b/test app.py\t\n"
            "@@ -1 +1 @@\n-old\n+pass\n"
        ), ["test app.py::test_widget"]),
        ((
            "diff --git a/src/helper.py b/spec/helper file.js\n"
            "similarity index 100%\n"
            "rename from src/helper.py\n"
            "rename to spec/helper file.js\n"
        ), []),
        ((
            "diff --git a/spec/blob data.bin b/spec/blob data.bin\n"
            "new file mode 100644\n"
            "index 0000000..0123456\n"
            "GIT binary patch\n"
        ), []),
    ),
)
def test_eval_selection_filters_spaced_rename_and_binary_test_paths(tmp_path, patch, targets):
    namespace = _remote_namespace(tmp_path)

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1", "FAIL_TO_PASS": targets},
        {"model_patch": patch},
        {},
    )

    assert selection["ok"] is True
    assert selection["model_patch"] == ""
    assert selection["filtered_patch_paths"]


def test_eval_selection_keeps_source_names_that_only_start_with_test(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/src/testing_helpers.py b/src/testing_helpers.py\n"
        "--- a/src/testing_helpers.py\n"
        "+++ b/src/testing_helpers.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    selection = namespace["prepare_eval_patch_selection"](
        {"instance_id": "task-1", "FAIL_TO_PASS": []},
        {"model_patch": patch},
        {},
    )

    assert selection["ok"] is True


def test_candidate_source_path_preserves_a_literal_b_directory_segment(tmp_path):
    namespace = _remote_namespace(tmp_path)
    path = "src/x b/module.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\t\n"
        f"+++ b/{path}\t\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    assert namespace["eval_python_source_paths"]({"model_patch": patch}) == [path]


def test_filter_model_patch_keeps_candidate_pytest_control_file_for_rejection(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/conftest.py b/conftest.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1 @@\n"
        "+pytest_plugins = ['candidate_plugin']\n"
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered, evidence = namespace["filter_model_patch_with_evidence"](patch)

    assert "conftest.py" in filtered
    assert "src/widget.py" in filtered
    assert evidence == []


def test_filter_model_patch_decodes_quoted_octal_git_paths(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        'diff --git "a/src/\\346\\250\\241\\345\\235\\227.py" '
        '"b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '--- "a/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '+++ "b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/test_\\344\\270\\255.py" "b/test_\\344\\270\\255.py"\n'
        '--- "a/test_\\344\\270\\255.py"\n'
        '+++ "b/test_\\344\\270\\255.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/src\\\\tests\\\\module.py" "b/src\\\\tests\\\\module.py"\n'
        '--- "a/src\\\\tests\\\\module.py"\n'
        '+++ "b/src\\\\tests\\\\module.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    filtered, evidence = namespace["filter_model_patch_with_evidence"](patch)

    assert "\\346\\250\\241\\345\\235\\227.py" in filtered
    assert "\\344\\270\\255.py" not in filtered
    assert "src\\\\tests\\\\module.py" not in filtered
    assert evidence == ["test_中.py", "src/tests/module.py"]


def test_filter_model_patch_preserves_source_identity_for_yarn_state(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
        "diff --git a/src/widget.ts b/src/widget.ts\n"
        "--- a/src/widget.ts\n"
        "+++ b/src/widget.ts\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    prediction = {"model_patch": patch}

    filtered = namespace["filter_model_patch_for_eval"](patch)
    evidence = namespace["model_patch_filter_evidence"](prediction)

    assert filtered == patch
    assert evidence == {
        "source_patch_sha256": namespace["patch_sha"](patch),
        "eval_patch_sha256": namespace["patch_sha"](patch),
        "filtered_patch_paths": [],
    }


def test_filter_model_patch_never_rewrites_a_stored_candidate(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc "
        "b/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and "
        "b/openlibrary/solr/__pycache__/query_utils.cpython-311.pyc differ\n"
        "diff --git a/openlibrary/solr/__pycache__/metadata.json "
        "b/openlibrary/solr/__pycache__/metadata.json\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/openlibrary/solr/__pycache__/metadata.json\n"
        "@@ -0,0 +1 @@\n"
        "+{}\n"
        "diff --git a/build/standalone.pyc b/build/standalone.pyc\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "Binary files /dev/null and b/build/standalone.pyc differ\n"
        "diff --git a/build/legacy.pyo b/build/legacy.pyo\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "Binary files /dev/null and b/build/legacy.pyo differ\n"
        "diff --git a/openlibrary/solr/query_utils.py "
        "b/openlibrary/solr/query_utils.py\n"
        "--- a/openlibrary/solr/query_utils.py\n"
        "+++ b/openlibrary/solr/query_utils.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/openlibrary/solr/cache.pyc.py "
        "b/openlibrary/solr/cache.pyc.py\n"
        "--- a/openlibrary/solr/cache.pyc.py\n"
        "+++ b/openlibrary/solr/cache.pyc.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/openlibrary/solr/__pycache___helper.py "
        "b/openlibrary/solr/__pycache___helper.py\n"
        "--- a/openlibrary/solr/__pycache___helper.py\n"
        "+++ b/openlibrary/solr/__pycache___helper.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    prediction = {"model_patch": patch}

    filtered = namespace["filter_model_patch_for_eval"](patch)
    evidence = namespace["model_patch_filter_evidence"](prediction)

    assert filtered == patch
    assert evidence == {
        "source_patch_sha256": namespace["patch_sha"](patch),
        "eval_patch_sha256": namespace["patch_sha"](patch),
        "filtered_patch_paths": [],
    }


def test_filter_model_patch_keeps_all_recorded_runtime_artifacts(tmp_path):
    namespace = _remote_namespace(tmp_path)

    def added(path):
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            "+generated\n"
        )

    patch = (
        added(".hypothesis/constants/a")
        + added(".pytest_cache/v/cache/nodeids")
        + added("pkg/.pytest_cache/kept")
        + added("qutebrowser/keyinput/keyutils.py")
    )

    filtered, evidence = namespace["filter_model_patch_with_evidence"](patch)

    assert filtered == patch
    assert evidence == []

    prediction = {"model_patch": patch}
    assert namespace["eval_python_source_paths"](prediction) == [
        "qutebrowser/keyinput/keyutils.py"
    ]


def test_yarn_install_state_only_legacy_record_is_not_rewritten(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    patch = (
        "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
    )
    prediction = {
        "instance_id": task,
        "model_patch": patch,
        "record_id": "record-1",
        "patch_sha256": namespace["patch_sha"](patch),
    }
    metric = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": namespace["patch_sha"](patch),
    }

    assert namespace["eval_model_patch"](prediction) == patch
    assert namespace["historical_generation_identity_status"](
        prediction,
        metric,
        task,
    ) == "invalid"


def test_eval_attempt_count_uses_the_source_patch_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    generated = (
        "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
    )
    source = (
        generated
        + "diff --git a/src/widget.ts b/src/widget.ts\n"
        "--- a/src/widget.ts\n"
        "+++ b/src/widget.ts\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": namespace["patch_sha"](source),
        "model_patch": source,
    }
    run_dir = namespace["base_run_dir"] / task
    source_sha = namespace["patch_sha"](source)
    child_sha = namespace["patch_sha"](namespace["eval_model_patch"](prediction))
    image_id = "sha256:" + "9" * 64
    _write_jsonl(
        run_dir / "eval_attempts.jsonl",
        [
            {
                "phase": "eval_attempt_started",
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
                "eval_image_id": image_id,
            },
            {
                "phase": "eval_attempt_started",
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
                "eval_patch_sha256": child_sha,
                "eval_image_id": image_id,
            },
        ],
    )

    assert child_sha == source_sha
    assert namespace["eval_attempt_count"](
        run_dir,
        prediction,
        task,
        expected_eval_image_id=image_id,
    ) == 2


def test_eval_summary_reuse_accepts_the_bound_source_patch_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["direct_eval_done_has_execution_proof"] = lambda *args, **kwargs: True
    task = "instance_org__repo-1"
    source = (
        "diff --git a/.yarn/install-state.gz b/.yarn/install-state.gz\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/.yarn/install-state.gz differ\n"
        "diff --git a/src/widget.ts b/src/widget.ts\n"
        "--- a/src/widget.ts\n"
        "+++ b/src/widget.ts\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    source_sha = namespace["patch_sha"](source)
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": source_sha,
        "model_patch": source,
    }
    summary = {
        "task": task,
        "record_id": "record-1",
        "patch_sha256": source_sha,
        "eval_image_id": "sha256:" + "9" * 64,
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
    ) is True

    summary["eval_patch_sha256"] = "0" * 64
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
    ) is False


def test_eval_summary_reuse_requires_exact_eval_image_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["direct_eval_done_has_execution_proof"] = lambda *args, **kwargs: True
    task = "instance_org__repo-1"
    patch = "diff --git a/src/widget.py b/src/widget.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    summary = {
        "task": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
        "eval_image_id": "sha256:" + "a" * 64,
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
    ) is False
    summary.pop("eval_image_id")
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
    ) is False
    summary["eval_image_id"] = "sha256:" + "b" * 64
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
    ) is True


def test_eval_attempt_budget_is_isolated_by_eval_image_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    patch = "diff --git a/src/widget.py b/src/widget.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    run_dir = namespace["base_run_dir"] / task
    common = {
        "phase": "eval_attempt_started",
        "task": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
    }
    _write_jsonl(
        run_dir / "eval_attempts.jsonl",
        [
            {**common, "eval_image_id": "sha256:" + "a" * 64},
            common,
            {**common, "eval_image_id": "sha256:" + "b" * 64},
        ],
    )

    assert namespace["eval_attempt_count"](
        run_dir,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
    ) == 1


def test_eval_attempt_budget_is_isolated_by_eval_spec_identity(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    patch = "diff --git a/src/widget.py b/src/widget.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    run_dir = namespace["base_run_dir"] / task
    common = {
        "phase": "eval_attempt_started",
        "task": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
        "eval_image_id": "sha256:" + "a" * 64,
    }
    _write_jsonl(
        run_dir / "eval_attempts.jsonl",
        [
            {**common, "eval_spec_sha256": "b" * 64},
            {**common, "eval_spec_sha256": "c" * 64},
            common,
        ],
    )

    assert namespace["eval_attempt_count"](
        run_dir,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "a" * 64,
        expected_eval_spec_sha256="c" * 64,
    ) == 1
    assert namespace["eval_attempt_count"](
        run_dir,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "a" * 64,
        expected_eval_spec_sha256="short",
    ) == 3


def test_validated_eval_patch_rejects_selection_eval_spec_drift_without_execution(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    patch = "diff --git a/src/widget.py b/src/widget.py\n+fixed\n"
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": namespace["patch_sha"](patch),
        "model_patch": patch,
    }
    summary_path = tmp_path / "summary.json"

    for selection_spec in ("", "short", "b" * 64):
        result = namespace["validated_eval_patch"](
            row={"instance_id": task},
            prediction=prediction,
            metric={},
            pairing="record_id",
            eval_spec_sha256="a" * 64,
            summary_path=summary_path,
            patch_selection={"eval_spec_sha256": selection_spec},
        )

        assert result["ready"] is False
        assert result["result"]["executed"] is False
        assert result["result"]["summary"]["technical_reasons"] == [
            "eval_spec_identity_mismatch"
        ]


def test_exhausted_eval_budget_reuses_only_current_eval_spec_summary(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    current_spec = "a" * 64
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": "b" * 64,
        "model_patch": "patch",
    }
    selection = {
        "eval_patch_sha256": "b" * 64,
        "image_id": "sha256:" + "c" * 64,
        "eval_spec_sha256": current_spec,
    }
    summary = {"eval_spec_sha256": "d" * 64}
    namespace["generation_done"] = lambda *args, **kwargs: (
        True,
        prediction,
        {},
        "record_id",
    )
    namespace["verified_plan_patch_selection"] = lambda *args: selection
    namespace["eval_attempt_count"] = lambda *args, **kwargs: 2
    namespace["load_json"] = lambda *args: summary
    namespace["eval_summary_matches_prediction"] = (
        lambda previous, *args, **kwargs: previous.get("eval_spec_sha256")
        == kwargs.get("eval_spec_sha256")
    )

    old = namespace["eval_for_task_with_retries"](
        {"instance_id": task}, lambda *args: (_ for _ in ()).throw(AssertionError("executed"))
    )
    assert old["status"] == "technical_eval_failed"
    assert old["retry_budget_exhausted"] is True

    summary["eval_spec_sha256"] = current_spec
    current = namespace["eval_for_task_with_retries"](
        {"instance_id": task}, lambda *args: (_ for _ in ()).throw(AssertionError("executed"))
    )
    assert current["status"] == "eval_done"
    assert current["retry_budget_exhausted"] is False
