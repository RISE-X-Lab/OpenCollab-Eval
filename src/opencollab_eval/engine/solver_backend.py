"""Named solver configurations consumed by evaluation commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_MODEL_RUNTIME_OPTIONS = (
    ("--model-name", "OPENCOLLAB_SWE_MODEL_NAME"),
    ("--llm-model", "OPENCOLLAB_SWE_LLM_MODEL"),
)


@dataclass(frozen=True, slots=True)
class WorkflowSolverSpec:
    """Configuration for a solver implemented by an OpenCollab workflow."""

    name: str
    workflow_name: str
    description: str
    max_attempts: int = 1
    default_budget_tokens: int | None = None
    required_runtime_options: tuple[tuple[str, str], ...] = ()
    config_overrides: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)


DEFAULT_WORKFLOW_SOLVERS: dict[str, WorkflowSolverSpec] = {
    "g11": WorkflowSolverSpec(
        name="g11",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "g1.1": WorkflowSolverSpec(
        name="g1.1",
        workflow_name="validation-council-solve",
        description="G1.1 validation council cooperation strategy.",
        max_attempts=3,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "baseTeam": WorkflowSolverSpec(
        name="baseTeam",
        workflow_name="base-team",
        description="Analyst, coder, and tester as a deterministic workflow.",
        max_attempts=1,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
    ),
    "TeamPro": WorkflowSolverSpec(
        name="TeamPro",
        workflow_name="team-pro",
        description="Dynamic analyst-led reconnaissance and phased coder/tester workflow.",
        max_attempts=3,
        default_budget_tokens=4_000_000,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
        config_overrides={
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32_768,
        },
    ),
    "openhands": WorkflowSolverSpec(
        name="openhands",
        workflow_name="openhands-external",
        description="External OpenHands solver invoked by a configured command template.",
        max_attempts=2,
        default_budget_tokens=16_000_000,
        required_runtime_options=_MODEL_RUNTIME_OPTIONS,
        config_overrides={
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32_768,
        },
        args={
            "openhands_command": (
                '"$OPENCOLLAB_REMOTE_REPO/src/opencollab_eval/resources/run_openhands_cli.sh" '
                "--headless --json --override-with-envs --file {prompt_file}"
            ),
            "max_steps": 120,
            "openhands_empty_patch_rejections": 2,
            "max_empty_patch_retries": 1,
            "max_eval_attempts": 2,
        },
    ),
}


def workflow_solver_spec(name: str) -> WorkflowSolverSpec:
    return DEFAULT_WORKFLOW_SOLVERS[name]


__all__ = [
    "DEFAULT_WORKFLOW_SOLVERS",
    "WorkflowSolverSpec",
    "workflow_solver_spec",
]
