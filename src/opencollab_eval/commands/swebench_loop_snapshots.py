"""Bounded native session inspection for the loop monitor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from opencollab import OpenCollab

from opencollab_eval.safe_files import regular_path_identity


def native_snapshot_inputs(path: Path) -> dict[Path, tuple[int, int, int, int, int] | None]:
    """Account for the base and journal before reading either input."""
    return {
        candidate: regular_path_identity(candidate) if os.path.lexists(candidate) else None
        for candidate in (path, Path(f"{path}.journal"))
    }


def read_native_snapshot(
    path: Path, *, identities: dict[Path, tuple[int, int, int, int, int] | None], max_file_bytes: int,
) -> dict[str, Any]:
    """Replay the owning reader while retaining monitor size and mutation checks."""
    for candidate, identity in identities.items():
        if identity is not None and identity[2] > max_file_bytes:
            raise ValueError(f"input exceeds {max_file_bytes}-byte limit: {candidate}")
    snapshot = OpenCollab.read_session_snapshot(path)
    for candidate, identity in identities.items():
        current = regular_path_identity(candidate) if os.path.lexists(candidate) else None
        if current != identity:
            raise OSError(f"session input changed between accounting and read: {candidate}")
    return snapshot
