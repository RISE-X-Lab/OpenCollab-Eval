"""Fail-closed coverage for Python targets without an external result boundary."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest

from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof


def test_prolite_test_command_never_falls_back_to_a_passing_noop(tmp_path):
    namespace = _remote_namespace(tmp_path)
    command = namespace["prolite_test_command"]
    is_runnable = namespace["_is_runnable_test_command"]

    assert command({"repo_language": "python"}, []) == ""
    assert command({"repo_language": "python"}, ["tests/test_widget.py::test_widget"]) == ""
    assert command({"repo_language": "ruby"}, ["spec/widget_spec.rb"]) == ""
    assert not is_runnable("")
    assert not is_runnable("true")
    assert not is_runnable(" : ")
    assert not is_runnable("echo ok")


def _legacy_pytest_plan(target: str) -> dict:
    return {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "pytest",
        "coverage": "exact_targets",
        "coverage_verified": True,
        "declared_targets": [target],
        "target_batches": [[target]],
        "commands": [f"pytest -q {target}"],
        "proofs": [{"kind": "pytest_structured_reports", "targets": [target]}],
    }


def _legacy_pytest_report(*, status: int) -> dict:
    target = "tests/test_widget.py::test_widget"
    plan = _legacy_pytest_plan(target)
    evidence = {
        "status": status,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "artifact_safe": True,
        "target_proof_matches_plan": status == 0,
        "target_failure_proof_matches_plan": status != 0,
    }
    return {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "resolved": status == 0,
        "technical_reasons": [],
        "output_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "eval_spec_sha256": "a" * 64,
        "tests_status": {
            "base_commit_status": 0,
            "service_bootstrap_status": 0,
            "before_repo_status": 0,
            "post_before_base_status": 0,
            "model_patch_status": 0,
            "test_patch_status": 0,
            "fail_to_pass_status": status,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": plan,
            "pass_to_pass_plan": {
                "schema": "opencollab.prolite_test_plan.v2",
                "adapter": "unsupported",
                "coverage": "none",
                "coverage_verified": False,
                "declared_targets": [],
                "target_batches": [],
                "commands": [],
                "proofs": [],
            },
            "fail_to_pass_evidence": [evidence],
            "pass_to_pass_evidence": [],
        },
    }


@pytest.mark.parametrize(
    "row",
    [
        {"repo_language": "python"},
        {"repo": "qutebrowser/qutebrowser", "repo_language": "python"},
        {},
    ],
)
def test_python_targets_require_an_external_result_boundary(tmp_path, row):
    namespace = _remote_namespace(tmp_path)
    target = "tests/test_widget.py::test_widget"

    plan = namespace["prolite_test_plan"](row, [target])

    assert plan["adapter"] == "unsupported"
    assert plan["declared_targets"] == [target]
    assert plan["coverage_verified"] is False
    assert plan["commands"] == []
    assert plan["proofs"] == []


def test_legacy_python_plan_script_exits_with_technical_status(tmp_path):
    namespace = _remote_namespace(tmp_path)

    script = namespace["prolite_test_plan_script"](
        _legacy_pytest_plan("tests/test_widget.py::test_widget"),
        "f2p",
    )

    assert "in-process Pytest evidence is unsupported" in script
    assert script.endswith("exit 86\n")
    assert "pytest -q" not in script


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("reported pass", 0),
        ("assertion failure", 1),
        ("zero collection", 5),
        ("import failure", 2),
        ("abrupt exit", 86),
    ],
)
def test_python_worker_outcomes_never_become_direct_eval_green(scenario, status):
    assert scenario
    assert direct_eval_done_has_execution_proof(_legacy_pytest_report(status=status)) is False
