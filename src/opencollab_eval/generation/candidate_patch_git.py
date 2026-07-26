"""Git primitives shared by trusted candidate construction and verification."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .candidate_patch_files import CandidateConstructionError
from .gen_prediction_patch_git import bounded_git_output

_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def clean_git_environment(index: Path, object_directory: Path, git_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_ATTR_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_INDEX_FILE=str(index),
        GIT_LITERAL_PATHSPECS="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OBJECT_DIRECTORY=str(object_directory),
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str(git_dir / "objects"),
        LC_ALL="C",
    )
    return env


def git_command(git: str, git_dir: Path, worktree: Path | None, *args: str) -> list[str]:
    command = [git, f"--git-dir={git_dir}"]
    if worktree is not None:
        command.append(f"--work-tree={worktree}")
    return [
        *command,
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]


def run_git(
    command: list[str], *, env: dict[str, str], timeout: float, payload: bytes | None = None
) -> None:
    result = subprocess.run(
        command,
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise CandidateConstructionError(
            f"trusted candidate Git command failed with exit {result.returncode}"
        )


def git_output(
    git: str,
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    limit: int,
    label: str,
    payload: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    try:
        return bounded_git_output(
            git,
            command[1:],
            env=env,
            timeout=timeout,
            max_bytes=limit,
            label=label,
            input_bytes=payload,
            allowed_returncodes=allowed_returncodes,
        )
    except RuntimeError as exc:
        raise CandidateConstructionError(str(exc)) from exc


def canonicalize_candidate_patch(
    *, git: str, git_dir: Path, base: str, patch: str, timeout: float = 120
) -> tuple[str, str]:
    """Return the tree and canonical patch produced by one trusted temporary index."""
    tree, canonical, _paths, _modes = project_candidate_patch(
        git=git, git_dir=git_dir, base=base, patch=patch, timeout=timeout
    )
    return tree, canonical


def _raw_diff(output: bytes) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    records, paths, modes = output.split(b"\0"), [], []
    index = 0
    while index < len(records) and records[index]:
        if index + 1 >= len(records):
            raise CandidateConstructionError("candidate projection raw diff is malformed")
        fields = records[index].split()
        path = os.fsdecode(records[index + 1])
        pure = PurePosixPath(path)
        if (
            len(fields) != 5
            or not fields[0].startswith(b":")
            or pure.is_absolute()
            or not pure.parts
            or ".." in pure.parts
        ):
            raise CandidateConstructionError("candidate projection raw diff is malformed")
        paths.append(pure.as_posix())
        modes.append(
            (pure.as_posix(), fields[0][1:].decode("ascii"), fields[1].decode("ascii"))
        )
        index += 2
    return tuple(paths), tuple(modes)


def project_candidate_patch(
    *, git: str, git_dir: Path, base: str, patch: str, timeout: float = 120
) -> tuple[str, str, tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    """Project a patch through one trusted index and return its full identity."""
    if not _OID_RE.fullmatch(base):
        raise CandidateConstructionError("trusted candidate base identity is invalid")
    with tempfile.TemporaryDirectory(prefix="opencollab-candidate-projection-") as temporary:
        root = Path(temporary)
        index, objects = root / "index", root / "objects"
        objects.mkdir()
        env = clean_git_environment(index, objects, git_dir)
        common = git_command(git, git_dir, None)
        run_git([*common, "read-tree", base], env=env, timeout=timeout)
        if patch:
            run_git(
                [*common, "apply", "--cached", "--binary", "--whitespace=nowarn", "-"],
                env=env,
                timeout=timeout,
                payload=patch.encode("utf-8"),
            )
        tree = git_output(
            git,
            [*common, "write-tree"],
            env=env,
            timeout=timeout,
            limit=256,
            label="candidate projection tree",
        ).decode("ascii").strip()
        raw = git_output(
            git,
            [*common, "diff", "--cached", "--raw", "-z", "--no-renames", base, "--"],
            env=env,
            timeout=timeout,
            limit=64 * 1024 * 1024,
            label="candidate projection path census",
        )
        canonical = git_output(
            git,
            [
                *common,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                base,
                "--",
            ],
            env=env,
            timeout=timeout,
            limit=64 * 1024 * 1024,
            label="canonical candidate projection",
        ).decode("utf-8", errors="strict")
    if not _OID_RE.fullmatch(tree):
        raise CandidateConstructionError("trusted candidate tree identity is invalid")
    paths, modes = _raw_diff(raw)
    return tree, canonical, paths, modes


__all__ = [
    "canonicalize_candidate_patch",
    "clean_git_environment",
    "git_command",
    "git_output",
    "project_candidate_patch",
    "run_git",
]
