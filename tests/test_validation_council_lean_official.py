from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from opencollab.workflows import CandidateRun

from opencollab_eval.workflows import _validation_council_lean_official_defs as defs
from opencollab_eval.workflows import _validation_council_lean_official_impl as implementation
from opencollab_eval.workflows.validation_council_lean_official import (
    validation_council_lean_official_v1,
)


class _Context:
    def __init__(self, candidate: CandidateRun) -> None:
        self.candidate = candidate
        self.phases: list[str] = []
        self.candidate_args: dict[str, Any] | None = None
        self.candidate_workflow_fn: Any = None
        self.adopted: CandidateRun | None = None
        self.preserve_paths: list[str] = []

    async def phase(self, title: str) -> None:
        self.phases.append(title)

    async def log(self, _message: str) -> None:
        return None

    async def candidate_workflow(
        self,
        workflow_fn: Any,
        args: dict[str, Any],
        *,
        label: str,
        budget: int | None,
    ) -> CandidateRun:
        assert label == "validation-council-lean-official"
        assert budget is None
        self.candidate_workflow_fn = workflow_fn
        self.candidate_args = args
        return self.candidate

    async def adopt_candidate(
        self,
        candidate: CandidateRun,
        *,
        preserve_paths: list[str],
    ) -> None:
        self.adopted = candidate
        self.preserve_paths = preserve_paths

    def tokens_spent(self) -> int:
        return 123


def _candidate(*, diff: str, output: dict[str, Any] | None = None) -> CandidateRun:
    return CandidateRun(
        label="candidate",
        output=output,
        diff=diff,
        test_records=(),
        verified_targets=(),
    )


@pytest.mark.asyncio
async def test_complete_council_runs_in_candidate_workspace_and_adopts() -> None:
    candidate = _candidate(
        diff="diff --git a/source.py b/source.py\n",
        output={"status": "done", "rounds": 1},
    )
    ctx = _Context(candidate)
    result = await validation_council_lean_official_v1(
        ctx,
        {
            "goal": "fix the issue",
            "FAIL_TO_PASS": ["hidden::test"],
            "test_patch": "hidden patch",
            "injected_test_paths": ["tests/injected.py"],
        },
    )

    assert ctx.candidate_workflow_fn is implementation.run_validation_council_lean_candidate
    assert ctx.candidate_args is not None
    assert "FAIL_TO_PASS" not in ctx.candidate_args
    assert "test_patch" not in ctx.candidate_args
    assert ctx.adopted is candidate
    assert ctx.preserve_paths == ["tests/injected.py"]
    assert result["status"] == "done"
    assert result["candidate_adopted"] is True
    assert result["tokens_spent"] == 123


@pytest.mark.asyncio
async def test_empty_candidate_is_not_adopted() -> None:
    ctx = _Context(_candidate(diff="", output={"status": "incomplete"}))

    result = await validation_council_lean_official_v1(ctx, {"goal": "fix"})

    assert ctx.adopted is None
    assert result["status"] == "incomplete"
    assert result["candidate_adopted"] is False


def test_port_uses_no_private_runtime_recovery_interface() -> None:
    source = inspect.getsource(implementation)
    for private_name in (
        "checkpoint_tree",
        "restore_tree",
        "session_count",
        "latest_evidence_ledger",
        "_factory",
    ):
        assert private_name not in source


def test_role_tool_surfaces_keep_bash_without_removed_run_tests() -> None:
    coder_names = {tool.name for tool in defs._coder_tools()}
    tester_names = {tool.name for tool in defs._tester_tools()}

    assert {"bash", "file_read", "file_write", "apply_patch", "grep", "git_diff"} <= coder_names
    assert {"bash", "file_read", "grep", "git_diff"} <= tester_names
    assert "run_tests" not in coder_names | tester_names


def test_new_modules_stay_within_repository_size_policy() -> None:
    workflow_dir = Path(implementation.__file__).parent
    for path in workflow_dir.glob("*validation_council_lean_official*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 800
