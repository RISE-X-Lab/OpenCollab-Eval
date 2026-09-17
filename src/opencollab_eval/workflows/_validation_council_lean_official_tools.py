"""Role tool adapters for the OC-compatible lean validation council."""

from __future__ import annotations

import os
import shlex
from typing import Any

from opencollab.tools import Tool

from ._public_api import toolset

_CANONICAL_TREE_GIT_MUTATORS = frozenset(
    {
        "add",
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "mv",
        "rebase",
        "reset",
        "restore",
        "rm",
        "stash",
        "switch",
        "update-index",
        "worktree",
    }
)
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {"-C", "-c", "--exec-path", "--git-dir", "--namespace", "--work-tree"}
)


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def _mutating_git_subcommand(command: str) -> str | None:
    tokens = _shell_tokens(command)
    for git_index, token in enumerate(tokens):
        if os.path.basename(token) != "git":
            continue
        index = git_index + 1
        while index < len(tokens):
            candidate = tokens[index]
            if candidate in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
                index += 2
                continue
            if candidate.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--exec-path=")):
                index += 1
                continue
            if candidate.startswith("-"):
                index += 1
                continue
            if candidate in _CANONICAL_TREE_GIT_MUTATORS:
                return candidate
            break
    return None


class _VerifierBashTool:
    """Preserve the verifier's Bash access while rejecting Git tree mutators."""

    name = "bash"
    default_timeout = 120.0
    disable_outer_timeout = False
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Inspection or test command."},
            "timeout": {"type": "number", "description": "Timeout in seconds."},
        },
        "required": ["command"],
    }
    description = (
        "Run inspection and test commands in the isolated candidate workspace. "
        "Git commands that alter the working tree are rejected; use git_diff to inspect changes."
    )

    def __init__(self, delegate: Tool) -> None:
        self._delegate = delegate

    def to_openai_schema(self) -> dict[str, Any]:
        return self._delegate.to_openai_schema()

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        command = str(params.get("command") or "")
        mutator = _mutating_git_subcommand(command)
        if mutator:
            return (
                "Error: verifier Bash cannot run Git commands that mutate the candidate workspace "
                f"(blocked: git {mutator}). Test the current patch in place and use git_diff for inspection."
            )
        result = await self._delegate.execute_with_runtime(params, runtime)
        return str(result)


def verifier_toolset(*names: str) -> list[Tool]:
    """Return the existing verifier surface with the historical Bash facade."""
    tools = toolset(*names)
    return [
        _VerifierBashTool(tool) if getattr(tool, "name", "") == "bash" else tool
        for tool in tools
    ]


__all__ = ["toolset", "verifier_toolset"]
