"""Bounded, descriptor-safe I/O shared by SWE report scripts."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from opencollab_eval import safe_files as files

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 128 * 1024 * 1024


def ensure_directory(path: Path) -> None:
    """Create one directory hierarchy while rejecting symlink components."""
    files.ensure_directory_no_symlinks(path)


def load_json_with_error(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read one bounded regular JSON object and retain the failure reason."""
    try:
        text = files.read_regular_text(path, max_bytes=MAX_REPORT_BYTES)
        value = json.loads(text)
    except FileNotFoundError:
        return {}, "missing_report_file"
    except UnicodeDecodeError:
        return {}, "invalid_report_encoding"
    except json.JSONDecodeError:
        return {}, "invalid_report_json"
    except (OSError, ValueError):
        return {}, "unsafe_or_unstable_report_file"
    if not isinstance(value, dict):
        return {}, "report_root_not_object"
    return value, None


def load_json(path: Path) -> dict[str, Any]:
    """Read one bounded regular JSON object, returning an empty object on failure."""
    value, _error = load_json_with_error(path)
    return value


def read_text(path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> str:
    """Read one bounded regular text file without following symlinks."""
    return files.read_regular_text(path, max_bytes=max_bytes)


def read_bytes(path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> bytes:
    """Read one bounded regular file without following symlinks."""
    return files.read_regular_bytes(path, max_bytes=max_bytes)


def write_text(path: Path, value: str, *, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Atomically replace one regular text file without following symlinks."""
    payload = value.encode("utf-8")
    files.write_regular_bytes_atomic(path, payload, max_bytes=max_bytes)


def write_json(path: Path, value: Any) -> None:
    """Atomically persist one bounded JSON report."""
    write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        max_bytes=MAX_REPORT_BYTES,
    )


@contextmanager
def open_regular_binary(path: Path, *, context: str) -> Iterator[BinaryIO]:
    """Check a regular stream's visible identity and parent again after reading."""
    path = Path(os.path.abspath(os.fspath(path)))
    parent_fd = files.open_directory_no_symlinks(path.parent)
    fd = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"{context} is not a regular file: {path}")
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"{context} is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"{context} changed while opening: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            yield handle
            after = os.fstat(handle.fileno())
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            identities = [
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                for info in (opened, after, current)
            ]
            if identities[0] != identities[1] or identities[1] != identities[2]:
                raise OSError(f"{context} changed while reading: {path}")
            if not files.directory_handle_matches_path(path.parent, parent_fd):
                raise OSError(f"{context} parent changed while reading: {path}")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


__all__ = [
    "MAX_LOG_BYTES",
    "MAX_REPORT_BYTES",
    "ensure_directory",
    "load_json",
    "load_json_with_error",
    "open_regular_binary",
    "read_bytes",
    "read_text",
    "write_json",
    "write_text",
]
