from __future__ import annotations

import pytest

from opencollab_eval.workflows._public_api import toolset


def test_toolset_applies_explicit_workflow_result_cap(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", "1200")

    tools = {tool.name: tool for tool in toolset("file_read", "grep", "bash")}

    assert tools["file_read"].max_read_chars == 1200
    assert tools["grep"].max_grep_chars == 1200
    assert tools["bash"].max_output_chars == 1200


def test_toolset_applies_per_tool_workflow_result_caps(monkeypatch) -> None:
    monkeypatch.setenv(
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS",
        "default=1024,git_diff=4096,bash=3072,run_tests=4096",
    )

    tools = {
        tool.name: tool
        for tool in toolset("file_read", "grep", "git_diff", "bash", "run_tests")
    }

    assert tools["file_read"].max_read_chars == 1024
    assert tools["grep"].max_grep_chars == 1024
    assert tools["git_diff"].max_diff_chars == 4096
    assert tools["git_diff"].max_status_chars == 4096
    assert tools["bash"].max_output_chars == 3072
    assert tools["run_tests"].max_traceback_chars == 4096


def test_toolset_leaves_unspecified_tools_at_sdk_defaults(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", "git_diff=4096")

    tools = {tool.name: tool for tool in toolset("file_read", "git_diff")}

    assert tools["file_read"].max_read_chars > 4096
    assert tools["git_diff"].max_diff_chars == 4096


def test_toolset_keeps_sdk_defaults_without_result_cap(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", raising=False)

    tool = toolset("file_read")[0]

    assert tool.max_read_chars > 1200


@pytest.mark.parametrize(
    "value",
    [
        "nope",
        "0",
        "255",
        "1000001",
        "unknown=1200",
        "file_read=nope",
        "file_read=1200,file_read=1300",
        "default=1200,broken",
    ],
)
def test_toolset_rejects_invalid_workflow_result_cap(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", value)

    with pytest.raises(ValueError, match="OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"):
        toolset("file_read")
