"""Shared bounded Git helpers for trusted candidate construction."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Protocol

from opencollab_eval.engine.swe_generation_proof import MAX_TRUSTED_PATCH_BYTES


class _Snapshot(Protocol):
    removed_gitlinks: tuple[tuple[str, str], ...]


class GitlinkContentChanged(RuntimeError):
    """The visible source state of a materialized Gitlink changed."""


_PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0


def _kill_and_reap(process, *, label: str) -> None:
    """Kill a child and reap it without allowing cleanup to hang forever."""
    try:
        process.kill()
    except ProcessLookupError:
        # It may have exited in the poll/kill race; still reap the child so a
        # short-lived generation attempt cannot leave a zombie behind.
        pass
    try:
        process.wait(timeout=_PROCESS_KILL_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} did not exit after SIGKILL") from exc


def run_git(
    git: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> None:
    result = subprocess.run(
        [git, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"trusted host git command failed: {args[0]}")


def bounded_git_output(
    git: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
    label: str,
    input_bytes: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    """Run Git with bounded stdout, discarded stderr, and a hard deadline."""
    input_file = None
    if input_bytes is not None:
        input_file = tempfile.TemporaryFile()
        input_file.write(input_bytes)
        input_file.seek(0)
    try:
        process = subprocess.Popen(
            [git, *args],
            stdin=input_file if input_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except BaseException:
        if input_file is not None:
            input_file.close()
        raise
    if process.stdout is None:
        try:
            _kill_and_reap(process, label="trusted host git")
        finally:
            if input_file is not None:
                input_file.close()
        raise RuntimeError("trusted host git did not expose patch output")
    timer_cleanup_errors: list[BaseException] = []

    def kill_on_timeout() -> None:
        try:
            _kill_and_reap(process, label=f"trusted host {label} timeout cleanup")
        except BaseException as exc:
            # The caller owns the exception path; retain the bounded cleanup
            # failure so it can be raised on the caller thread.
            timer_cleanup_errors.append(exc)

    timer = threading.Timer(timeout, kill_on_timeout)
    chunks: list[bytes] = []
    total = 0
    cleanup_complete = False
    timer.start()
    try:
        while chunk := process.stdout.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                _kill_and_reap(process, label=f"trusted host {label} cleanup")
                cleanup_complete = True
                raise RuntimeError(f"trusted host {label} exceeded its byte limit")
            chunks.append(chunk)
        if timer_cleanup_errors:
            raise timer_cleanup_errors[0]
        returncode = process.wait(timeout=_PROCESS_KILL_REAP_TIMEOUT_SECONDS)
    except BaseException as exc:
        if not cleanup_complete:
            try:
                _kill_and_reap(process, label=f"trusted host {label} cleanup")
            except BaseException as cleanup_error:
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(
                        "process cleanup failed after generation error: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise cleanup_error from exc
        raise
    finally:
        timer.cancel()
        process.stdout.close()
        if input_file is not None:
            input_file.close()
    if returncode not in allowed_returncodes:
        raise RuntimeError(f"trusted host {label} failed with exit {returncode}")
    return b"".join(chunks)


def _safe_gitlink_root(root: Path, path: str) -> Path:
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError("snapshot contains an unsafe Gitlink path")
    return root.joinpath(*relative.parts)


def strip_nested_git_metadata(root: Path) -> None:
    """Remove nested repository metadata while keeping ordinary source bytes."""
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == root:
            directories[:] = [name for name in directories if name != ".git"]
        elif ".git" in directories:
            marker = current_path / ".git"
            marker.unlink() if marker.is_symlink() else shutil.rmtree(marker)
            directories.remove(".git")
        if ".git" in files:
            (current_path / ".git").unlink()


def prepare_gitlink_state_repositories(
    root: Path,
    snapshot: _Snapshot,
    destination: Path,
    git: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> tuple[tuple[str, Path], ...]:
    """Index candidate-visible materialized Gitlink baselines without forced adds."""
    if not snapshot.removed_gitlinks:
        return ()
    destination.mkdir()
    repositories: list[tuple[str, Path]] = []
    object_format = ["--object-format=sha256"] if len(snapshot.removed_gitlinks[0][1]) == 64 else []
    for index, (path, _oid) in enumerate(snapshot.removed_gitlinks):
        worktree = _safe_gitlink_root(root, path)
        if not worktree.is_dir() or not any(worktree.iterdir()):
            continue
        git_dir = destination / str(index)
        run_git(
            git,
            ["init", "--bare", "--quiet", *object_format, str(git_dir)],
            env=env,
            timeout=timeout,
        )
        info = git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text(
            "* -text -filter -ident -working-tree-encoding\n", encoding="utf-8"
        )
        common = [f"--git-dir={git_dir}", f"--work-tree={worktree}"]
        for key, value in (
            ("core.attributesFile", os.devnull),
            ("core.autocrlf", "false"),
            ("core.excludesFile", os.devnull),
            ("core.fsmonitor", "false"),
            ("core.hooksPath", os.devnull),
            ("commit.gpgSign", "false"),
            ("user.email", "gitlink-baseline@invalid"),
            ("user.name", "OpenCollab Gitlink Baseline"),
        ):
            run_git(git, [*common, "config", key, value], env=env, timeout=timeout)
        run_git(git, [*common, "add", "-A", "--", "."], env=env, timeout=timeout)
        run_git(
            git,
            [*common, "commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "baseline"],
            env=env,
            timeout=timeout,
        )
        repositories.append((path, git_dir))
    return tuple(repositories)


def gitlink_repository_digest(
    git_dir: Path,
    git: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> str:
    """Bind the visible, non-ignored Gitlink baseline without reading ignored files."""
    tree = bounded_git_output(
        git,
        [f"--git-dir={git_dir}", "rev-parse", "HEAD^{tree}"],
        env=env,
        timeout=timeout,
        max_bytes=256,
        label="materialized Gitlink tree identity",
    ).strip()
    if not tree or any(byte not in b"0123456789abcdef" for byte in tree):
        raise RuntimeError("materialized Gitlink tree identity is invalid")
    return hashlib.sha256(tree).hexdigest()


def verify_materialized_gitlink(
    root: Path,
    path: str,
    git: str,
    *,
    baseline_git_dir: Path,
    env: dict[str, str],
    timeout: float,
) -> None:
    """Ignore runtime residue while detecting candidate-visible source changes."""
    worktree = _safe_gitlink_root(root, path)
    if not worktree.is_dir():
        raise GitlinkContentChanged("materialized Gitlink is no longer a directory")
    status = bounded_git_output(
        git,
        [
            f"--git-dir={baseline_git_dir}",
            f"--work-tree={worktree}",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        env=env,
        timeout=timeout,
        max_bytes=MAX_TRUSTED_PATCH_BYTES,
        label="materialized Gitlink status",
    )
    if any(status.split(b"\0")):
        raise GitlinkContentChanged("materialized Gitlink source content changed")


__all__ = [
    "GitlinkContentChanged",
    "bounded_git_output",
    "gitlink_repository_digest",
    "prepare_gitlink_state_repositories",
    "run_git",
    "strip_nested_git_metadata",
    "verify_materialized_gitlink",
]
