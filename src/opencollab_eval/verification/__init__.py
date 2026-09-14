"""Native OC tools with evaluation-only observation of executed test evidence."""

from opencollab.tools import BuiltinToolName, Tool, builtin_tools

from .bash_evidence import BashEvidence

EvaluationToolName = BuiltinToolName


def evaluation_tools(
    *names: BuiltinToolName,
    headless: bool = True,
    allow_file_creation: bool = True,
) -> tuple[Tool, ...]:
    tools = builtin_tools(*names, headless=headless, allow_file_creation=allow_file_creation)
    return tuple(BashEvidence(tool) if tool.name == "bash" else tool for tool in tools)
