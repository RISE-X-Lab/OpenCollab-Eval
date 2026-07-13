"""Workspace conversion helpers for evaluation adapters."""

from __future__ import annotations

from collections.abc import Callable

from opencollab.sdk import ExecutionEnvironment, attach_workspace

from opencollab_eval.engine.eval_adapter.models import PreparedWorkspace


def docker_environment_for_workspace(
    workspace: PreparedWorkspace,
    *,
    command_prefix: Callable[[str], str] | str | None = None,
    timeout_returncode: int = -1,
) -> ExecutionEnvironment:
    return attach_workspace(
        container_id=workspace.container_id,
        repo_root=workspace.repo_root,
        command_prefix=command_prefix,
        timeout_returncode=timeout_returncode,
    )


__all__ = ["docker_environment_for_workspace"]
