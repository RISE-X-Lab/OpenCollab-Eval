from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from typing import Any

import pytest
from evaluator_test_support import (
    EvalResult,
    EvalTask,
    ExecResult,
    FakeEnv,
    is_worktree_diff_cmd,
    patch_evaluator_llm,
    run,
    run_eval_task,
)
from opencollab.environments import local_environment as LocalEnvironment

from opencollab_eval.engine import evaluator
from opencollab_eval.engine import swe_checkpoint as checkpoint_mod
from opencollab_eval.engine.swe_checkpoint import WorktreeCheckpoint
from opencollab_eval.engine.workflows import generate_review_fix

__all__ = [
    "Any",
    "CheckpointEnv",
    "EvalResult",
    "EvalTask",
    "ExecResult",
    "FakeEnv",
    "LocalEnvironment",
    "ScriptedCtx",
    "WorktreeCheckpoint",
    "asyncio",
    "checkpoint_mod",
    "evaluator",
    "generate_review_fix",
    "hashlib",
    "is_worktree_diff_cmd",
    "json",
    "os",
    "patch_evaluator_llm",
    "pytest",
    "run",
    "run_eval_task",
    "seed_checkpoint",
    "subprocess",
    "uuid",
]


class CheckpointEnv(FakeEnv):
    def __init__(self, diff="diff --git a/x b/x\n+checkpoint\n", diff_outputs=None):
        super().__init__(diff=diff)
        self.writes: list[tuple[str, str]] = []
        self.diff_outputs = list(diff_outputs or [])

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd):
            stdout = self.diff_outputs.pop(0) if self.diff_outputs else self.diff
            return ExecResult(returncode=0, stdout=stdout, stderr="")
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def write_file(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        await self.write_file(path, content)
        return path


def seed_checkpoint(
    checkpoint: WorktreeCheckpoint,
    patch: str,
    *,
    submission_eligible: bool = True,
    status: str = "written",
) -> None:
    checkpoint.patch_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.patch_path.write_text(patch, encoding="utf-8")
    checkpoint.meta_path.write_text(
        json.dumps(
            {
                "schema": "opencollab.swe_worktree_checkpoint.v1",
                "status": status,
                "patch_bytes": len(patch.encode("utf-8", errors="surrogatepass")),
                "patch_sha256": checkpoint_mod._patch_sha(patch),
                "submission_eligible": submission_eligible,
                "preserved_previous_patch": status == "failed",
            }
        ),
        encoding="utf-8",
    )


class ScriptedCtx:
    """A minimal WorkflowContext stand-in scripting agent() replies."""

    def __init__(self, env: Any, replies: list[Any]) -> None:
        self.env = env
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False):
        self.agent_calls.append({"prompt": prompt, "schema": schema, "label": label})
        return self._replies.pop(0)

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        pass
