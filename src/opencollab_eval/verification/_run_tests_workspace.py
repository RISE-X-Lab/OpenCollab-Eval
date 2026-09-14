"""Keep test discovery, execution, and pytest selectors in one workspace."""

from __future__ import annotations

import posixpath
import shlex
from typing import Any


def workspace_target(target: str, workspace: str | None) -> str:
    """Preserve the full path and selector while using pytest's invocation root."""
    if not workspace or not target:
        return target
    path, separator, selector = target.partition("::")
    if posixpath.isabs(path):
        path = posixpath.relpath(path, workspace)
    elif path:
        path = posixpath.normpath(path)
    return path + separator + selector


class WorkspaceTestEnvironment:
    """Re-enter the declared workspace after any image shell initialization."""

    def __init__(self, environment: Any, workspace: str):
        self.environment = environment
        self.workspace = workspace

    def command(self, command: str) -> str:
        return f"cd -- {shlex.quote(self.workspace)} && {command}"

    async def exec_cmd(self, command: str, timeout: float = 120.0) -> Any:
        return await self.environment.exec_cmd(self.command(command), timeout=timeout)


def workspace_environment(environment: Any) -> Any:
    workspace = getattr(environment, "workspace", None)
    if isinstance(workspace, str) and posixpath.isabs(workspace):
        return WorkspaceTestEnvironment(environment, workspace)
    return environment
