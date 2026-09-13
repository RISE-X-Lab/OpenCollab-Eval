"""Evaluation-owned tools complementing OpenCollab's public built-ins."""

from __future__ import annotations

from typing import Literal

from opencollab.tools import BuiltinToolName, Tool, builtin_tools

from .run_tests import RunTestsTool

EvaluationToolName = BuiltinToolName | Literal["run_tests"]


def evaluation_tools(
    *names: EvaluationToolName,
    headless: bool = True,
    allow_file_creation: bool = True,
) -> tuple[Tool, ...]:
    """Keep exact-test proof local to Eval; ordinary tools use OC's public API."""
    if len(set(names)) != len(names):
        raise ValueError("evaluation tool names must be unique")
    ordinary = iter(
        builtin_tools(
            *(name for name in names if name != "run_tests"),
            headless=headless,
            allow_file_creation=allow_file_creation,
        )
    )
    return tuple(
        RunTestsTool(
            allow_runner_override=not headless,
            allow_extra_args=not headless,
            require_process_isolation=headless,
        )
        if name == "run_tests"
        else next(ordinary)
        for name in names
    )
