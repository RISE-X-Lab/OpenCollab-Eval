#!/usr/bin/env python3
"""Enforce size and line-count limits for files added by a Git change."""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

MAX_BYTES = 512_000
MAX_PY_LINES = 800


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        env=_git_environment(),
    ).stdout


def _added_paths(repository: Path, base: str, head: str) -> list[str]:
    output = _git(
        repository,
        "diff",
        "--diff-filter=A",
        "--name-only",
        "-z",
        base,
        head,
    )
    return [os.fsdecode(raw_path) for raw_path in output.split(b"\0") if raw_path]


def _tree_entries(repository: Path, head: str) -> dict[bytes, tuple[str, str, str]]:
    entries: dict[bytes, tuple[str, str, str]] = {}
    output = _git(repository, "ls-tree", "-r", "-z", "--full-tree", head)
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        entries[raw_path] = (mode, object_type, object_id)
    return entries


def _blob(repository: Path, object_id: str) -> bytes:
    return _git(repository, "cat-file", "blob", object_id)


def _blob_size(repository: Path, object_id: str) -> int:
    return int(_git(repository, "cat-file", "-s", object_id).decode("ascii").strip())


def _command_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _command_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _error(path: str, message: str) -> None:
    print(
        f"::error file={_command_property(path)}::{_command_message(message)}",
        flush=True,
    )


def check_added_files(
    repository: Path,
    base: str,
    head: str,
    *,
    max_bytes: int = MAX_BYTES,
    max_py_lines: int = MAX_PY_LINES,
    require_files: bool = False,
) -> list[tuple[str, str]]:
    """Return path and message pairs for regular files added by the change."""
    violations: list[tuple[str, str]] = []
    added_paths = _added_paths(repository, base, head)
    if require_files and not added_paths:
        return [
            ("repository", "no files were available for the complete-tree check")
        ]
    entries = _tree_entries(repository, head)
    for relative in added_paths:
        entry = entries.get(os.fsencode(relative))
        if entry is None:
            raise ValueError(f"added path is missing from the head tree: {relative!r}")
        mode, object_type, object_id = entry
        if object_type != "blob" or not mode.startswith("100"):
            continue
        size = _blob_size(repository, object_id)
        if size > max_bytes:
            violations.append(
                (relative, f"new file is {size} bytes, limit is {max_bytes}")
            )
        if relative.endswith(".py") and size <= max_bytes:
            content = _blob(repository, object_id)
            lines = content.count(b"\n") + int(
                bool(content) and not content.endswith(b"\n")
            )
            if lines > max_py_lines:
                violations.append(
                    (
                        relative,
                        f"new Python module is {lines} lines, limit is {max_py_lines}",
                    )
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--require-files", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    repository = Path.cwd()
    try:
        violations = check_added_files(
            repository,
            args.base,
            args.head,
            require_files=args.require_files,
        )
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"Git object inspection failed: {exc}", file=sys.stderr)
        return 2
    if not violations:
        print("Added-file hygiene checks passed.")
        return 0
    for path, message in violations:
        _error(path, message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
