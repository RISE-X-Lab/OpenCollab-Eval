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


def test_prolite_python_plan_fails_before_container_execution(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "python-task"
    _seed_remote_completed_generation(namespace, task)
    namespace["ensure_image"] = lambda image: pytest.fail(
        "unsupported Python plans must fail before image selection"
    )

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["tests/test_many.py::test_case"],
            "repo_language": "python",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["resolved"] is False
    assert result["summary"]["technical_reasons"] == ["no_verified_fail_to_pass_plan"]


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
    assert result["summary"]["resolved"] is False
    assert result["summary"]["technical_reasons"] == ["no_verified_fail_to_pass_plan"]


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
