"""Compatibility names for OpenCollab's native public test-evidence tools."""

from opencollab.tools import BashEvidence, BuiltinToolName, evidence_tools

EvaluationToolName = BuiltinToolName
evaluation_tools = evidence_tools

__all__ = ["BashEvidence", "EvaluationToolName", "evaluation_tools"]
