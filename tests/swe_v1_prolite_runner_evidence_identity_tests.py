"""Candidate identity and evaluation-summary reuse tests."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import _remote_namespace


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
        "candidate_expectation": {"run_identity_sha256": "a" * 64},
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
        expected_candidate_expectation=summary["candidate_expectation"],
    ) is True

    summary["eval_patch_sha256"] = "0" * 64
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
        expected_candidate_expectation=summary["candidate_expectation"],
    ) is False


def test_eval_summary_reuse_rejects_candidate_expectation_drift(tmp_path):
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
    expectation_a = {"run_identity_sha256": "a" * 64}
    expectation_b = {"run_identity_sha256": "b" * 64}
    summary = {
        "task": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
        "eval_image_id": "sha256:" + "9" * 64,
        "candidate_expectation": expectation_a,
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
        expected_candidate_expectation=expectation_a,
    ) is True
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "9" * 64,
        expected_candidate_expectation=expectation_b,
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
        "candidate_expectation": {"run_identity_sha256": "a" * 64},
    }

    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
        expected_candidate_expectation=summary["candidate_expectation"],
    ) is False
    summary.pop("eval_image_id")
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
        expected_candidate_expectation=summary["candidate_expectation"],
    ) is False
    summary["eval_image_id"] = "sha256:" + "b" * 64
    assert namespace["eval_summary_matches_prediction"](
        summary,
        prediction,
        task,
        expected_eval_image_id="sha256:" + "b" * 64,
        expected_candidate_expectation=summary["candidate_expectation"],
    ) is True


def test_exhausted_eval_budget_passes_candidate_expectation_to_real_matcher(tmp_path):
    """Exercise the actual matcher so retry wiring cannot hide a kwarg mismatch."""
    namespace = _remote_namespace(tmp_path)
    task = "instance_org__repo-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    expectation = {"run_identity_sha256": "a" * 64}
    prediction = {
        "instance_id": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    selection = {
        "ok": True,
        "eval_patch_sha256": patch_sha,
        "image_id": "sha256:" + "b" * 64,
        "eval_spec_sha256": "c" * 64,
        "candidate_expectation": expectation,
    }
    summary = {
        "task": task,
        "record_id": "record-1",
        "patch_sha256": patch_sha,
        "eval_patch_sha256": patch_sha,
        "eval_image_id": selection["image_id"],
        "candidate_expectation": expectation,
    }
    namespace["generation_done"] = lambda *args, **kwargs: (
        True,
        prediction,
        {},
        "record_id",
    )
    namespace["verified_plan_patch_selection"] = lambda *args: selection
    namespace["eval_attempt_count"] = lambda *args, **kwargs: 2
    namespace["load_json"] = lambda *args: summary
    namespace["direct_eval_done_has_execution_proof"] = lambda *args, **kwargs: True

    result = namespace["eval_for_task_with_retries"](
        {"instance_id": task},
        lambda *args: (_ for _ in ()).throw(AssertionError("executed")),
    )

    assert result["status"] == "eval_done"
    assert result["retry_budget_exhausted"] is False
