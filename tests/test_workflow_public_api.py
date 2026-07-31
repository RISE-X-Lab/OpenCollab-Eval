from __future__ import annotations

import pytest

from opencollab_eval.workflows._public_api import toolset


def test_toolset_applies_explicit_workflow_result_cap(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", "1200")

    tools = {tool.name: tool for tool in toolset("file_read", "grep", "bash")}

    assert tools["file_read"].max_read_chars == 1200
    assert tools["grep"].max_grep_chars == 1200
    assert tools["bash"].max_output_chars == 1200


def test_toolset_keeps_sdk_defaults_without_result_cap(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", raising=False)

    tool = toolset("file_read")[0]

    assert tool.max_read_chars > 1200


@pytest.mark.parametrize("value", ["nope", "0", "255", "1000001"])
def test_toolset_rejects_invalid_workflow_result_cap(monkeypatch, value: str) -> None:
    monkeypatch.setenv("OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS", value)

    with pytest.raises(ValueError, match="OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS"):
        toolset("file_read")
