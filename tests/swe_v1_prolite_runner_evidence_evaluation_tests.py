"""Official evaluation plan and execution evidence tests."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    _seed_remote_completed_generation,
    _test_only_patch,
    _write_jsonl,
    pytest,
)


def test_prolite_prediction_sha_comes_from_patch_text(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    stale_patch = "diff --git a/src/a.py b/src/a.py\n+stale\n"
    current_patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    stale_sha = namespace["patch_sha"](stale_patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "model_patch": current_patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert namespace["row_patch_sha"](prediction) == namespace["patch_sha"](current_patch)
    assert done is False
    assert pairing == "record_id_patch_sha_mismatch"


def test_remote_patch_sha_match_requires_exact_hex_digest(tmp_path):
    namespace = _remote_namespace(tmp_path)
    digest = "a1" * 32

    assert namespace["patch_sha_matches"](digest, digest) is True
    assert namespace["patch_sha_matches"](digest[:12], digest) is False
    assert namespace["patch_sha_matches"]("g" * 64, "g" * 64) is False
    assert namespace["patch_sha_matches"](digest.upper(), digest) is False


def test_prolite_python_plan_reaches_external_boundary_before_image_execution(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "python-task"
    _seed_remote_completed_generation(namespace, task)
    observed = []

    def missing_image(image):
        observed.append(image)
        return {"ok": False, "reason": "missing_image"}

    namespace["ensure_image"] = missing_image

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_many.py::test_case"],
            "repo_language": "python",
        }
    )

    assert observed
    assert result["status"] == "blocked_missing_eval_image"
    assert result["executed"] is False
    assert result["attempt_count"] == 0


def test_prolite_eval_marks_ruby_echo_ok_as_technical_red(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "ruby-task"
    _seed_remote_completed_generation(namespace, task)
    namespace["ensure_image"] = lambda image: pytest.fail("unverified commands must fail before Docker")

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["spec/widget_spec.rb"],
            "repo_language": "ruby",
            "test_cmd": "echo ok",
            "eval_cmd": "echo ok",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["executed"] is False
    assert result["attempt_count"] == 0
    assert result["summary"]["resolved"] is False
    assert result["summary"]["technical_reasons"] == ["no_verified_fail_to_pass_plan"]


def test_pre_execution_failure_is_not_retried_without_state_change(tmp_path):
    namespace = _remote_namespace(tmp_path)
    prediction = {
        "instance_id": "task",
        "record_id": "record",
        "model_patch": "diff --git a/a.py b/a.py\n+fixed\n",
    }
    namespace["generation_done"] = lambda *args, **kwargs: (
        True,
        prediction,
        {"instance_id": "task", "record_id": "record"},
        "record_id",
    )
    namespace["verified_plan_patch_selection"] = lambda *args: {
        "ok": True,
        "eval_patch_sha256": "a" * 64,
        "image_id": "sha256:" + "b" * 64,
    }
    namespace["eval_attempt_count"] = lambda *args, **kwargs: 0
    calls = []

    def fail_before_execution(row, selection=None):
        calls.append((row, selection))
        return {"status": "technical_eval_failed", "executed": False}

    result = namespace["eval_for_task_with_retries"](
        {"instance_id": "task"}, fail_before_execution
    )

    assert len(calls) == 1
    assert result["attempt_count"] == 0
    assert "attempts" not in result


def test_remote_runner_does_not_reuse_stale_done_for_test_only_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    stale_summary = {
        "status": "done",
        "task": task,
        "patch_sha256": patch_sha,
        "record_id": "r1",
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](stale_summary, prediction, task) is False


def test_remote_runner_rejects_identity_only_done_summary_without_test_evidence(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": namespace["patch_sha"](patch),
        "model_patch": patch,
    }
    f2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_x.py::test_target"],
    )
    p2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [],
    )
    eval_spec_sha256 = namespace["prolite_eval_spec_sha256"](
        {},
        f2p_plan,
        p2p_plan,
    )
    identity_only = {
        "status": "done",
        "task": task,
        "patch_sha256": prediction["patch_sha256"],
        "record_id": "r1",
        "eval_spec_sha256": eval_spec_sha256,
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](
        identity_only,
        prediction,
        task,
        eval_spec_sha256=eval_spec_sha256,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
    ) is False


def test_eval_spec_binds_base_script_and_workspace_helpers(tmp_path):
    namespace = _remote_namespace(tmp_path)
    f2p_plan = {"commands": ["pytest -q test_x.py"], "coverage_verified": True}
    p2p_plan = {"commands": [], "coverage_verified": True}

    def digest(*, base="a", script="script-a", helper=b"helper-a"):
        return namespace["prolite_eval_spec_sha256"](
            {"base_commit": base * 40},
            f2p_plan,
            p2p_plan,
            script_source=script,
            helper_sources={"workspace.py": helper},
        )

    original = digest()
    assert digest(base="b") != original
    assert digest(script="script-b") != original
    assert digest(helper=b"helper-b") != original
