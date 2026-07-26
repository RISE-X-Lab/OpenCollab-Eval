"""Filesystem checks for trusted candidate construction."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from opencollab_eval.patch_paths import is_generated_runtime_artifact_path


class CandidateConstructionError(RuntimeError):
    """The current task worktree cannot be represented by a trusted Git patch."""


def validate_parent_chain(worktree: Path, path: str) -> None:
    current = worktree
    for part in PurePosixPath(path.rstrip("/")).parts[:-1]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CandidateConstructionError(f"candidate path {path} has an unsafe parent")


def validate_file(worktree: Path, path: str, max_file_bytes: int | None) -> os.stat_result:
    validate_parent_chain(worktree, path)
    info = (worktree / path).lstat()
    if stat.S_ISLNK(info.st_mode):
        return info
    if not stat.S_ISREG(info.st_mode):
        raise CandidateConstructionError(f"candidate {path} is not representable in a Git patch")
    if stat.S_IMODE(info.st_mode) & 0o444 == 0:
        raise CandidateConstructionError(f"candidate {path} is unreadable")
    if max_file_bytes is not None and info.st_size > max_file_bytes:
        raise CandidateConstructionError(f"candidate {path} exceeded its file byte limit")
    return info


def special_files(
    worktree: Path, ignored_paths: tuple[str, ...], max_entries: int
) -> tuple[str, ...]:
    ignored = {path.rstrip("/") for path in ignored_paths}
    entries = 0
    special: list[str] = []
    for current, directories, files in os.walk(worktree, topdown=True, followlinks=False):
        relative_root = Path(current).relative_to(worktree)
        retained: list[str] = []
        for name in directories:
            relative = (relative_root / name).as_posix()
            parts = PurePosixPath(relative).parts
            if any(
                part in {".git", ".opencollab"} or part.startswith(".opencollab-retired-")
                for part in parts
            ) or relative in ignored or is_generated_runtime_artifact_path(relative):
                continue
            entries += 1
            if entries > max_entries:
                raise CandidateConstructionError("filesystem census exceeded its entry limit")
            retained.append(name)
        directories[:] = retained
        root = Path(current)
        for name in files:
            relative = (root / name).relative_to(worktree).as_posix()
            parts = PurePosixPath(relative).parts
            if any(
                part in {".git", ".opencollab"} or part.startswith(".opencollab-retired-")
                for part in parts
            ) or relative in ignored or is_generated_runtime_artifact_path(relative):
                continue
            entries += 1
            if entries > max_entries:
                raise CandidateConstructionError("filesystem census exceeded its entry limit")
            info = (root / name).lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                special.append(relative)
    return tuple(special)


def flatten_hardlinks(
    worktree: Path, paths: tuple[str, ...], max_file_bytes: int
) -> tuple[str, ...]:
    flattened: list[str] = []
    for path in paths:
        target = worktree / path
        if not os.path.lexists(target) or target.is_dir():
            continue
        info = validate_file(worktree, path, None)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
            continue
        validate_file(worktree, path, max_file_bytes)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".opencollab-hardlink-", dir=target.parent
        )
        try:
            destination = os.fdopen(descriptor, "wb")
            descriptor = -1
            with target.open("rb") as source, destination:
                shutil.copyfileobj(source, destination, length=64 * 1024)
                destination.flush()
                os.fchmod(destination.fileno(), stat.S_IMODE(info.st_mode))
                os.fsync(destination.fileno())
            os.replace(temporary, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
        flattened.append(path)
    return tuple(flattened)


def flatten_nested(
    paths: tuple[str, ...], worktree: Path
) -> tuple[bool, tuple[tuple[str, str], ...]]:
    flattened: list[tuple[str, str]] = []
    for path in paths:
        if not path.endswith("/"):
            continue
        marker = worktree / path.rstrip("/") / ".git"
        if not os.path.lexists(marker):
            raise CandidateConstructionError(f"untracked directory {path} has no visible files")
        info = marker.lstat()
        marker_type = (
            "symlink"
            if stat.S_ISLNK(info.st_mode)
            else "file"
            if stat.S_ISREG(info.st_mode)
            else "directory"
        )
        if marker_type in {"symlink", "file"}:
            marker.unlink()
        elif marker_type == "directory":
            shutil.rmtree(marker)
        else:
            raise CandidateConstructionError(f"nested repository marker {path}.git is unsafe")
        flattened.append((path.rstrip("/"), marker_type))
    return bool(flattened), tuple(flattened)


def reject_outward_symlinks(worktree: Path, paths: tuple[str, ...]) -> None:
    root = worktree.resolve(strict=True)
    for path in paths:
        if not os.path.lexists(worktree / path):
            continue
        validate_parent_chain(worktree, path)
        candidate = worktree / path
        if candidate.is_symlink():
            try:
                candidate.resolve(strict=False).relative_to(root)
            except ValueError as exc:
                raise CandidateConstructionError(
                    f"candidate symlink {path} escapes the worktree"
                ) from exc
