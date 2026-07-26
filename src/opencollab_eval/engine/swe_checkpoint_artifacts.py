"""Artifact metadata and path handling for SWE worktree checkpoints."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _workspace_roots(env: Any) -> tuple[Path, ...]:
    raw_roots = [
        env.workspace
        if env.local_filesystem
        else getattr(env, "host_workspace", None),
        getattr(env, "source_workspace", None),
    ]
    roots: list[Path] = []
    for raw_root in raw_roots:
        if not raw_root:
            continue
        try:
            root = Path(os.path.abspath(os.fspath(raw_root)))
        except (OSError, TypeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def workspace_relative_host_paths(
    env: Any,
    raw_path: str | os.PathLike[str],
) -> tuple[Path, ...]:
    """Map a host path through every declared host-side workspace root."""
    try:
        target = Path(os.path.abspath(os.fspath(raw_path)))
    except (OSError, TypeError, ValueError):
        return ()
    relative_paths: list[Path] = []
    for root in _workspace_roots(env):
        pairs = [(target, root)]
        try:
            pairs.append(
                (target.resolve(strict=False), root.resolve(strict=False))
            )
        except (OSError, RuntimeError):
            pass
        for candidate, candidate_root in pairs:
            try:
                relative = candidate.relative_to(candidate_root)
            except ValueError:
                continue
            if relative not in relative_paths:
                relative_paths.append(relative)
    return tuple(relative_paths)


def build_checkpoint_meta(
    *,
    status: str,
    reason: str,
    interval_seconds: float,
    patch_path: Path,
    patch_bytes: int = 0,
    patch_sha256: str = "",
    error: str = "",
    preserved_previous_patch: bool = False,
    submission_eligible: bool = False,
) -> dict[str, Any]:
    """Build the durable metadata record stored beside a checkpoint patch."""
    return {
        "schema": "opencollab.swe_worktree_checkpoint.v1",
        "status": status,
        "reason": reason,
        "captured_at": time.time(),
        "checkpoint_interval_seconds": interval_seconds,
        "loss_bound_seconds": interval_seconds,
        "patch_path": str(patch_path),
        "patch_bytes": patch_bytes,
        "patch_sha256": patch_sha256,
        "error": error,
        "preserved_previous_patch": preserved_previous_patch,
        "submission_eligible": submission_eligible,
    }


def checkpoint_artifact_exclude_paths(
    env: Any,
    artifact_paths: Sequence[Path],
) -> tuple[str, ...]:
    """Return checkpoint artifact paths relative to an environment workspace."""
    paths: dict[str, None] = {}
    for artifact_path in artifact_paths:
        for relative in workspace_relative_host_paths(env, artifact_path):
            if relative != Path("."):
                paths.setdefault(relative.as_posix(), None)
    return tuple(paths)
