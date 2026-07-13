from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from opencollab.sdk import RuntimeConfig, WorkflowRunResult

from opencollab_eval.contracts import PreparedWorkspace, PublicTask, SolverBudget
from opencollab_eval.solvers import OpenCollabWorkflowSolver


class FakeRuntime:
    def __init__(self) -> None:
        self.request = None

    async def run_workflow(self, request):
        self.request = request
        return WorkflowRunResult(
            output={"status": "done"},
            workflow_name="base-team",
            tokens_spent=17,
            session_count=2,
            artifact_dir=request.artifact_dir,
            manifest_path=request.artifact_dir / "workflow.json",
        )


async def test_solver_uses_sdk_and_returns_runtime_evidence(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    solver = OpenCollabWorkflowSolver(
        name="baseTeam",
        workflow_name="base-team",
        config=RuntimeConfig(model="model", provider="provider"),
        workflows_dir=Path(str(files("opencollab_eval.workflows"))),
        runtime=runtime,
    )
    task = PublicTask(
        task_id="solver-0123456789abcdef0123456789abcdef",
        repo="owner/repo",
        problem_statement="Fix it.",
        metadata={"nested": {"items": ["public"]}},
    )
    result = await solver.run(
        task,
        PreparedWorkspace(container_id="container-1", repo_root="/testbed"),
        tmp_path / "run",
        SolverBudget(max_tokens=100, timeout_seconds=30),
    )

    assert result.task_id == task.task_id
    assert result.tokens_spent == 17
    assert result.session_count == 2
    assert runtime.request.inputs["instance_id"] == task.task_id
    assert runtime.request.inputs["public_metadata"] == {"nested": {"items": ["public"]}}
    assert runtime.request.inputs["public_metadata"] is not task.metadata
    assert runtime.request.budget.max_tokens == 100
