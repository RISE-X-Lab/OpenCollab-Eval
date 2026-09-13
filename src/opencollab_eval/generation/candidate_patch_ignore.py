"""Project trusted baseline ignore rules into a controller-owned view."""

from __future__ import annotations

import os
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
    worktree: Path
    ignore_worktree: Path
    ignored_paths: tuple[str, ...]
    nested_roots: tuple[str, ...]


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


def _materialize_candidate_namespace(
    git: str,
    git_dir: Path,
    ignore_view: Path,
    namespace: Path,
    worktree: Path,
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    entry_limit: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ignored_paths: set[str] = set()
    nested_roots: set[str] = set()
    entries = 0

    def onerror(error: OSError) -> None:
        raise CandidateConstructionError(f"candidate directory is unreadable: {error}")

    for current, directories, files in os.walk(
        worktree, topdown=True, followlinks=False, onerror=onerror
    ):
        relative_root = Path(current).relative_to(worktree)
        has_nested_marker = relative_root != Path(".") and ".git" in {
            *directories,
            *files,
        }
        retained: list[str] = []
        directory_candidates: list[str] = []
        leaf_candidates: list[str] = []
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
                leaf_candidates.append(relative)
                continue
            retained.append(name)
            directory_candidates.append(relative + "/")
        ignored_directories = _ignored(
            git,
            git_dir,
            ignore_view,
            tuple(directory_candidates),
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
        )
        ignored_paths.update(ignored_directories)
        directories[:] = []
        for name in retained:
            relative = (relative_root / name).as_posix()
            if relative + "/" in ignored_directories:
                continue
            (namespace / relative).mkdir(parents=True, exist_ok=True)
            directories.append(name)
        entries += len(files)
        if entries > entry_limit:
            raise CandidateConstructionError("filesystem census exceeded its entry limit")
        leaf_candidates.extend(
            (relative_root / name).as_posix()
            for name in files
            if not any(
                part in {".git", ".opencollab"} or part.startswith(".opencollab-retired-")
                for part in PurePosixPath((relative_root / name).as_posix()).parts
            )
            and not is_generated_runtime_artifact_path((relative_root / name).as_posix())
        )
        ignored_files = _ignored(
            git,
            git_dir,
            ignore_view,
            tuple(leaf_candidates),
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
        )
        ignored_paths.update(ignored_files)
        for relative in leaf_candidates:
            if relative in ignored_files:
                continue
            destination = namespace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not os.path.lexists(destination):
                destination.touch()
        if has_nested_marker:
            nested_roots.add(relative_root.as_posix())
    return tuple(sorted(ignored_paths)), tuple(sorted(nested_roots))


@contextmanager
def trusted_ignore_view(
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
    """Build a controller-owned path view governed by baseline ignore files."""
    names = frozenset(control_names)
    if not names or not names.issubset({".gitignore", ".gitattributes"}):
        raise CandidateConstructionError("trusted control-file selection is invalid")
    with tempfile.TemporaryDirectory(
        prefix=".opencollab-ignore-", dir=worktree.parent
    ) as temporary:
        ignore_view = Path(temporary) / "ignore"
        namespace = Path(temporary) / "namespace"
        ignore_view.mkdir()
        namespace.mkdir()
        _materialize_baseline_ignores(
            git,
            git_dir,
            ignore_view,
            entries,
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
            control_names=names,
        )
        ignored_paths, nested_roots = _materialize_candidate_namespace(
            git,
            git_dir,
            ignore_view,
            namespace,
            worktree,
            env=env,
            timeout=timeout,
            byte_limit=byte_limit,
            entry_limit=entry_limit,
        )
        yield TrustedIgnoreState(namespace, ignore_view, ignored_paths, nested_roots)


__all__ = ["TrustedIgnoreState", "trusted_ignore_entries", "trusted_ignore_view"]
