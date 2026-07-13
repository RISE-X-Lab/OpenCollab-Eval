"""OpenCollab SDK-backed solver adapter."""

from __future__ import annotations

from pathlib import Path

from opencollab.sdk import (
    OpenCollabRuntime,
    RunBudget,
    RuntimeConfig,
    WorkflowRunRequest,
    attach_workspace,
    discover_workflows,
)

from opencollab_eval.contracts import PreparedWorkspace, PublicTask, SolverBudget, SolverRun, thaw_public_value


class OpenCollabWorkflowSolver:
    def __init__(
        self,
        *,
        name: str,
        workflow_name: str,
        config: RuntimeConfig,
        workflows_dir: Path,
        runtime: OpenCollabRuntime | None = None,
    ) -> None:
        self.name = name
        self.workflow_name = workflow_name
        self.config = config
        self.workflows_dir = workflows_dir
        self.runtime = runtime or OpenCollabRuntime()

    async def run(
        self,
        task: PublicTask,
        workspace: PreparedWorkspace,
        artifact_dir: Path,
        budget: SolverBudget,
    ) -> SolverRun:
        workflow_spec = discover_workflows(str(self.workflows_dir)).get(self.workflow_name)
        environment = attach_workspace(
            container_id=workspace.container_id,
            repo_root=workspace.repo_root,
        )
        result = await self.runtime.run_workflow(
            WorkflowRunRequest(
                workflow=workflow_spec,
                config=self.config,
                inputs={
                    "description": task.problem_statement,
                    "goal": task.problem_statement,
                    "instance_id": task.task_id,
                    "repo": task.repo,
                    "hints": list(task.hints),
                    "public_metadata": thaw_public_value(task.metadata),
                },
                budget=RunBudget(
                    max_tokens=budget.max_tokens,
                    timeout_seconds=budget.timeout_seconds,
                    max_concurrency=budget.max_concurrency,
                ),
                environment=environment,
                workspace=workspace.repo_root,
                artifact_dir=artifact_dir,
            )
        )
        if result.tokens_spent is None or result.session_count is None:
            raise RuntimeError("OpenCollab SDK returned no runtime accounting evidence")
        return SolverRun(
            task_id=task.task_id,
            solver_name=self.name,
            output=result.output,
            tokens_spent=result.tokens_spent,
            session_count=result.session_count,
            artifact_dir=artifact_dir,
            sdk_api_version=result.sdk_api_version,
        )


__all__ = ["OpenCollabWorkflowSolver"]
