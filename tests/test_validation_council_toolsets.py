from __future__ import annotations

from opencollab_eval.workflows.validation_council_solve import validation_council_solve


def _workflow_globals():
    return validation_council_solve.__globals__


def _names(tools) -> list[str]:
    return [tool.name for tool in tools]


def test_validation_council_read_tools_are_read_only():
    names = _names(_workflow_globals()["_read_tools"]())

    assert names == ["file_read", "grep"]
    assert "bash" not in names
    assert "file_write" not in names
    assert "apply_patch" not in names


def test_validation_council_tester_tools_can_probe_but_not_author():
    # The constrained runner supplies executable evidence without exposing a
    # general shell that could mutate source before candidate attribution.
    tools = _workflow_globals()["_tester_tools"]()
    names = _names(tools)

    assert names == ["file_read", "run_tests", "grep", "git_diff"]
    assert "bash" not in names
    assert "file_write" not in names
    assert "apply_patch" not in names


def test_validation_council_coder_can_edit_and_run_public_checks():
    tools = _workflow_globals()["_coder_tools"]()
    names = _names(tools)

    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "grep",
        "git_diff",
    ]
    run_tests = next(tool for tool in tools if tool.name == "run_tests")
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_validation_council_shared_rules_are_compact_and_keep_integrity_guards():
    rules = _workflow_globals()["SHARED_RULES"]

    assert len(rules.encode("utf-8")) <= 512
    assert "hidden grader data" in rules
    assert "official hidden tests" in rules
    assert "grader patches" in rules
    assert "FAIL_TO_PASS IDs" in rules
    assert "Obey this role and its tools" in rules
    assert "/tmp/opencollab-validation-*" in rules
    assert "smallest source fix" in rules
    assert "Do not run git commit" in rules
