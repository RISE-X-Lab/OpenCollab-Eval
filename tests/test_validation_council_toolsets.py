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
    # Gate roles must be able to run an executable probe (bash) so a PASS is
    # backed by a real check, not prose alone — removing it caused a silent
    # false green (2026-07-09 review). They still cannot author the fix.
    tools = _workflow_globals()["_tester_tools"]()
    names = _names(tools)

    assert names == ["bash", "file_read", "run_tests", "grep", "git_diff"]
    assert "file_write" not in names
    assert "apply_patch" not in names


def test_validation_council_risk_tools_can_read_the_diff():
    # An auditor with no tools (was []) cannot inspect what it judges — it must
    # at least read the diff and sources, while staying read-only.
    names = _names(_workflow_globals()["_risk_tools"]())
    assert "file_read" in names
    assert "git_diff" in names
    assert "bash" not in names
    assert "file_write" not in names


def test_validation_council_coder_tools_keep_edit_path():
    names = _names(_workflow_globals()["_coder_tools"]())

    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "grep",
    ]


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
