"""Expose baseline ignore rules without granting Solver ignore files authority."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opencollab_eval.patch_paths import is_generated_runtime_artifact_path

from .candidate_patch_files import CandidateConstructionError
from .candidate_patch_git import git_command, git_output


@dataclass(frozen=True, slots=True)
class TrustedIgnoreState:
    added_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]


def trusted_ignore_entries(
    git: str,
    git_dir: Path,
    base: str,
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
) -> tuple[tuple[str, str, str], ...]:
    output = git_output(
        git,
        [*git_command(git, git_dir, None), "ls-tree", "-rz", "--full-tree", base],
        env=env,
        timeout=timeout,
        limit=byte_limit,
        label="baseline ignore tree census",
    )
    entries: list[tuple[str, str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        path = os.fsdecode(raw_path)
        pure = PurePosixPath(path)
        if (
            not separator
            or len(fields) != 3
            or pure.name != ".gitignore"
            or pure.is_absolute()
            or not pure.parts
            or ".." in pure.parts
            or fields[0] not in {b"100644", b"100755", b"120000"}
            or fields[1] != b"blob"
        ):
            if separator and len(fields) == 3 and pure.name != ".gitignore":
                continue
            raise CandidateConstructionError("trusted baseline ignore tree is malformed")
        entries.append((fields[0].decode("ascii"), fields[2].decode("ascii"), pure.as_posix()))
    return tuple(entries)


def _ignored(
    git: str,
    git_dir: Path,
    view: Path,
    paths: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
) -> frozenset[str]:
    if not paths:
        return frozenset()
    safe_paths = tuple("./" + path for path in paths)
    payload = b"\0".join(os.fsencode(path) for path in safe_paths) + b"\0"
    if len(payload) > byte_limit:
        raise CandidateConstructionError("trusted ignore census exceeded its byte limit")
    ignore_env = dict(env)
    ignore_env.pop("GIT_LITERAL_PATHSPECS", None)
    output = git_output(
        git,
        [*git_command(git, git_dir, view), "check-ignore", "--no-index", "--stdin", "-z"],
        env=ignore_env,
        timeout=timeout,
        limit=byte_limit,
        label="trusted ignore census",
        payload=payload,
        allowed_returncodes=(0, 1),
    )
    return frozenset(
        os.fsdecode(record)[2:] if os.fsdecode(record).startswith("./") else os.fsdecode(record)
        for record in output.split(b"\0")
        if record
    )


def _materialize_baseline_ignores(
    git: str,
    git_dir: Path,
    view: Path,
    entries: tuple[tuple[str, str, str], ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    control_names: frozenset[str],
) -> frozenset[str]:
    paths: set[str] = set()
    for mode, oid, path in entries:
        if PurePosixPath(path).name not in control_names:
            continue
        destination = view / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = git_output(
            git,
            [*git_command(git, git_dir, None), "cat-file", "blob", oid],
            env=env,
            timeout=timeout,
            limit=byte_limit,
            label="baseline ignore file",
        )
        if mode == "120000":
            os.symlink(os.fsdecode(payload), destination)
        elif mode in {"100644", "100755"}:
            destination.write_bytes(payload)
        else:
            raise CandidateConstructionError("trusted baseline has an invalid ignore file")
        paths.add(path)
    return frozenset(paths)


def _visible_ignore_files(
    git: str,
    git_dir: Path,
    view: Path,
    worktree: Path,
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    entry_limit: int,
    control_names: frozenset[str],
) -> frozenset[str]:
    discovered: set[str] = set()
    entries = 0

    def onerror(error: OSError) -> None:
        raise CandidateConstructionError(f"candidate directory is unreadable: {error}")

    for current, directories, files in os.walk(
        worktree, topdown=True, followlinks=False, onerror=onerror
    ):
        relative_root = Path(current).relative_to(worktree)
        retained: list[str] = []
        candidates: list[str] = []
        for name in directories:
            relative = (relative_root / name).as_posix()
            parts = PurePosixPath(relative).parts
            if any(
                part in {".git", ".opencollab"} or part.startswith(".opencollab-retired-")
                for part in parts
            ) or is_generated_runtime_artifact_path(relative):
                continue
            entries += 1
            if entries > entry_limit:
                raise CandidateConstructionError("filesystem census exceeded its entry limit")
            if (worktree / relative).is_symlink():
                continue
            retained.append(name)
            candidates.append(relative + "/")
        ignored = _ignored(
            git,
            git_dir,
            view,
            tuple(candidates),
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
        )
        directories[:] = [
            name
            for name in retained
            if (relative_root / name).as_posix() + "/" not in ignored
        ]
        entries += len(files)
        if entries > entry_limit:
            raise CandidateConstructionError("filesystem census exceeded its entry limit")
        discovered.update(
            (relative_root / name).as_posix()
            for name in control_names.intersection(files)
            if not is_generated_runtime_artifact_path((relative_root / name).as_posix())
        )
    return frozenset(discovered)


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _copy_ignore(source: Path, destination: Path) -> None:
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    else:
        destination.write_bytes(source.read_bytes())
        os.chmod(destination, stat.S_IMODE(source.stat().st_mode))


def _same_ignore(left: Path, right: Path, byte_limit: int) -> bool:
    if not os.path.lexists(left) or not os.path.lexists(right):
        return False
    left_info, right_info = left.lstat(), right.lstat()
    if stat.S_IFMT(left_info.st_mode) != stat.S_IFMT(right_info.st_mode):
        return False
    if stat.S_ISLNK(left_info.st_mode):
        return os.readlink(left) == os.readlink(right)
    if not stat.S_ISREG(left_info.st_mode):
        return False
    if left_info.st_size > byte_limit or right_info.st_size > byte_limit:
        raise CandidateConstructionError("candidate ignore file exceeded its byte limit")
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError as exc:
        raise CandidateConstructionError(f"candidate ignore file is unreadable: {exc}") from exc


def _validate_existing_parents(worktree: Path, path: str) -> None:
    current = worktree
    for part in PurePosixPath(path).parts[:-1]:
        current /= part
        if not os.path.lexists(current):
            return
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CandidateConstructionError(f"candidate path {path} has an unsafe parent")


@contextmanager
def trusted_ignore_overlay(
    git: str,
    git_dir: Path,
    worktree: Path,
    entries: tuple[tuple[str, str, str], ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    entry_limit: int,
    control_names: tuple[str, ...] = (".gitignore",),
) -> Iterator[TrustedIgnoreState]:
    """Temporarily expose trusted baseline Git control files."""
    names = frozenset(control_names)
    if not names or not names.issubset({".gitignore", ".gitattributes"}):
        raise CandidateConstructionError("trusted control-file selection is invalid")
    with tempfile.TemporaryDirectory(
        prefix=".opencollab-ignore-", dir=worktree.parent
    ) as temporary:
        view, backup = Path(temporary) / "view", Path(temporary) / "backup"
        view.mkdir()
        backup.mkdir()
        baseline = _materialize_baseline_ignores(
            git,
            git_dir,
            view,
            entries,
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
            control_names=names,
        )
        visible = _visible_ignore_files(
            git,
            git_dir,
            view,
            worktree,
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
            entry_limit=entry_limit,
            control_names=names,
        )
        replaced: list[tuple[Path, Path | None]] = []
        created_directories: list[Path] = []
        try:
            changed = {
                path
                for path in baseline
                if not _same_ignore(worktree / path, view / path, byte_limit)
            }
            for path in sorted(baseline | visible):
                _validate_existing_parents(worktree, path)
                target, saved = worktree / path, backup / path
                if os.path.lexists(target):
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, saved)
                    saved_path: Path | None = saved
                else:
                    saved_path = None
                parent = target.parent
                missing: list[Path] = []
                while parent != worktree and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_directories.append(directory)
                source = view / path
                if os.path.lexists(source):
                    _copy_ignore(source, target)
                replaced.append((target, saved_path))
            added = tuple(sorted(path for path in visible if path not in baseline))
            yield TrustedIgnoreState(added, tuple(sorted(changed | set(added))))
        finally:
            for target, saved in reversed(replaced):
                if os.path.lexists(target):
                    _remove(target)
                if saved is not None and os.path.lexists(saved):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(saved, target)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass


__all__ = ["TrustedIgnoreState", "trusted_ignore_entries", "trusted_ignore_overlay"]
