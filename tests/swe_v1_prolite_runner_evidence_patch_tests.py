"""Patch filtering and candidate identity evidence tests."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    _test_only_patch,
    _write_jsonl,
)


def test_remote_runner_rejects_test_only_patch_before_eval(tmp_path):
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

    done, _prediction, _metric, _pairing = namespace["generation_done"](run_dir, task)
    result = namespace["eval_for_task"]({"instance_id": task})

    assert done is False
    assert result["status"] == "empty_eval_patch_invalid"
    assert result["summary"]["eval_model_patch_chars"] == 0
    assert result["summary"]["technical_reasons"] == ["empty_eval_patch_after_filter"]


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

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/app code.py" in filtered
    assert "tests/test app.py" not in filtered


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

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/\\346\\250\\241\\345\\235\\227.py" in filtered
    assert "test_\\344\\270\\255.py" not in filtered
    assert "src\\\\tests\\\\module.py" in filtered


def test_filter_model_patch_removes_yarn_install_state_with_parent_child_sha(tmp_path):
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

    assert ".yarn/install-state.gz" not in filtered
    assert "src/widget.ts" in filtered
    assert evidence == {
        "source_patch_sha256": namespace["patch_sha"](patch),
        "eval_patch_sha256": namespace["patch_sha"](filtered),
        "filtered_patch_paths": [
            {
                "path": ".yarn/install-state.gz",
                "reason": "generated_dependency_artifact",
            }
        ],
    }


def test_filter_model_patch_removes_python_bytecode_with_parent_child_sha(tmp_path):
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

    assert "query_utils.cpython-311.pyc" not in filtered
    assert "__pycache__/metadata.json" in filtered
    assert "build/standalone.pyc" in filtered
    assert "build/legacy.pyo" in filtered
    assert "openlibrary/solr/query_utils.py" in filtered
    assert "openlibrary/solr/cache.pyc.py" in filtered
    assert "openlibrary/solr/__pycache___helper.py" in filtered
    assert evidence == {
        "source_patch_sha256": namespace["patch_sha"](patch),
        "eval_patch_sha256": namespace["patch_sha"](filtered),
        "filtered_patch_paths": [
            {
                "path": "openlibrary/solr/__pycache__/query_utils.cpython-311.pyc",
                "reason": "generated_python_bytecode",
            },
        ],
    }


def test_filter_model_patch_removes_root_python_test_runtime_artifacts(tmp_path):
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

    assert ".hypothesis/" not in filtered
    assert "b/.pytest_cache/" not in filtered
    assert "pkg/.pytest_cache/kept" in filtered
    assert "qutebrowser/keyinput/keyutils.py" in filtered
    assert evidence == [
        {
            "path": ".hypothesis/constants/a",
            "reason": "generated_python_test_artifact",
        },
        {
            "path": ".pytest_cache/v/cache/nodeids",
            "reason": "generated_python_test_artifact",
        },
    ]

    prediction = {"model_patch": patch}
    assert namespace["eval_python_source_paths"](prediction) == [
        "qutebrowser/keyinput/keyutils.py"
    ]


def test_yarn_install_state_only_patch_is_not_a_completed_generation(tmp_path):
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

    assert namespace["eval_model_patch"](prediction) == ""
    assert namespace["historical_generation_identity_status"](
        prediction,
        metric,
        task,
    ) == "invalid"


def test_eval_attempt_count_uses_filtered_child_patch_identity(tmp_path):
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
    _write_jsonl(
        run_dir / "eval_attempts.jsonl",
        [
            {
                "phase": "eval_attempt_started",
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
            },
            {
                "phase": "eval_attempt_started",
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
                "eval_patch_sha256": child_sha,
            },
        ],
    )

    assert child_sha != source_sha
    assert namespace["eval_attempt_count"](run_dir, prediction, task) == 1


def test_eval_summary_reuse_requires_filtered_child_patch_identity(tmp_path):
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
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
    ) is False

    summary["eval_patch_sha256"] = namespace["patch_sha"](
        namespace["eval_model_patch"](prediction)
    )
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
    ) is True


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
