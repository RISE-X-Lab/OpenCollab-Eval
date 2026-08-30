"""Bounded evidence-file primitives owned by OpenCollab-Eval."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, TextIO

_LOCK_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 1024 * 1024

# macOS keeps these public spellings as root-owned compatibility symlinks.  An
# ``O_NOFOLLOW`` component walk quite correctly rejects them, but that would
# make the default ``tempfile`` location (usually under ``/var/folders``)
# unusable.  We canonicalize only these exact, independently validated aliases;
# every user-controlled component remains subject to the descriptor walk below.
_MACOS_SYSTEM_ALIASES = (
    (Path("/var"), Path("/private/var")),
    (Path("/tmp"), Path("/private/tmp")),
    (Path("/etc"), Path("/private/etc")),
)


class OwnedFileRetirementError(OSError):
    """An owned file could not be removed safely."""


class OwnedFileMismatchError(OwnedFileRetirementError):
    """The visible file no longer matches the caller's open descriptor."""


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = os.fspath(path)
    if not value or "\0" in value:
        raise ValueError("path must be non-empty text without NUL bytes")
    return _canonicalize_system_alias(Path(os.path.abspath(value)))


def _root_owned_non_writable_directory(path: Path) -> bool:
    """Check a canonical system directory without following a final link.

    ``/private/tmp`` is deliberately sticky and mode ``1777`` on macOS.  It is
    writable for creating new entries, but the sticky bit prevents an
    untrusted user from replacing another user's entries.  Permit that one
    well-known shape while rejecting ordinary group/other-writable directories.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode):
        return False
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        # A sticky root-owned directory (notably macOS's /private/tmp, 1777)
        # is safe as an alias anchor: users may create children but cannot
        # rename one another's entries.  Ordinary writable directories are not.
        return bool(mode & stat.S_ISVTX and mode & 0o002)
    return True


def _validated_system_alias(alias: Path, canonical: Path) -> bool:
    """Return whether ``alias`` is the trusted macOS spelling of ``canonical``."""
    try:
        alias_info = os.lstat(alias)
    except OSError:
        return False
    # Do not infer trust from a textual path alone: a user-created replacement
    # at one of these names must continue through the normal symlink rejection.
    if not stat.S_ISLNK(alias_info.st_mode) or alias_info.st_uid != 0:
        return False
    try:
        if Path(os.path.realpath(alias)) != canonical:
            return False
    except OSError:
        return False
    return _root_owned_non_writable_directory(canonical)


def _canonicalize_system_alias(path: Path) -> Path:
    if sys.platform != "darwin":
        return path
    for alias, canonical in _MACOS_SYSTEM_ALIASES:
        try:
            relative = path.relative_to(alias)
        except ValueError:
            continue
        if _validated_system_alias(alias, canonical):
            return canonical / relative
    return path


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def open_directory_no_symlinks(path: str | os.PathLike[str]) -> int:
    """Open a directory after rejecting symlink path components."""
    absolute = _absolute(path)
    fd = os.open(absolute.anchor or os.sep, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=fd)
            except OSError as exc:
                raise OSError(f"directory parent is not a real directory: {absolute}") from exc
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise NotADirectoryError(absolute)
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_directory_no_symlinks(path: str | os.PathLike[str]) -> None:
    """Create a directory tree while rejecting symlink components."""
    absolute = _absolute(path)
    fd = os.open(absolute.anchor or os.sep, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=fd)
            except OSError as exc:
                raise OSError(f"directory parent is not a real directory: {absolute}") from exc
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise NotADirectoryError(absolute)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def directory_path_matches_fd(path: str | os.PathLike[str], fd: int) -> bool:
    """Return whether ``fd`` and ``path`` identify the same directory."""
    try:
        current = os.stat(_absolute(path), follow_symlinks=False)
        opened = os.fstat(fd)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
        opened.st_dev,
        opened.st_ino,
    )


directory_handle_matches_path = directory_path_matches_fd


def _require_limit(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    return max_bytes


def _check_parent_identity(
    parent_fd: int,
    expected_parent_identity: tuple[int, int] | None,
    path: Path,
) -> None:
    if expected_parent_identity is None:
        return
    opened = os.fstat(parent_fd)
    if (opened.st_dev, opened.st_ino) != expected_parent_identity:
        raise OSError(f"parent identity changed: {path.parent}")


def read_regular_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bytes:
    """Read one regular file without following its final symlink."""
    limit = _require_limit(max_bytes)
    target = _absolute(path)
    parent_fd = open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        _check_parent_identity(parent_fd, expected_parent_identity, target)
        fd = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"read target is not a regular file: {target}")
        if opened.st_size > limit:
            raise ValueError(f"read target exceeds {limit}-byte limit: {target}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise ValueError(f"read target exceeds {limit}-byte limit: {target}")
        completed = os.fstat(fd)
        if (completed.st_size, completed.st_mtime_ns, completed.st_ctime_ns) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise OSError(f"read target changed while reading: {target}")
        return payload
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def read_regular_text(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    return read_regular_bytes(path, max_bytes=max_bytes).decode(encoding)


def regular_path_identity(
    path: str | os.PathLike[str],
) -> tuple[int, int, int, int, int]:
    """Return ordinary identity and mutation metadata for one regular path."""
    target = _absolute(path)
    info = os.stat(target, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"identity target is not a regular file: {target}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def regular_handle_matches_path(handle: TextIO, path: str | os.PathLike[str]) -> bool:
    """Return whether an open regular handle still has the visible identity."""
    try:
        opened = os.fstat(handle.fileno())
        current = os.stat(_absolute(path), follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(opened.st_mode) and stat.S_ISREG(current.st_mode) and (
        opened.st_dev,
        opened.st_ino,
    ) == (current.st_dev, current.st_ino)


def open_regular_text_append(path: str | os.PathLike[str]) -> TextIO:
    """Open or create a regular UTF-8 file for append without following links."""
    target = _absolute(path)
    ensure_directory_no_symlinks(target.parent)
    parent_fd = open_directory_no_symlinks(target.parent)
    fd = -1
    try:
        try:
            existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"append target is not a regular file: {target}")
        fd = os.open(
            target.name,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"append target is not a regular file: {target}")
        handle = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        return handle
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _acquire_append_lock(fd: int) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out acquiring append lock") from exc
            time.sleep(0.01)


def write_locked_text(handle: TextIO, text: str) -> None:
    """Append UTF-8 text while holding an advisory file lock."""
    fd = handle.fileno()
    _acquire_append_lock(fd)
    try:
        payload = memoryview(text.encode("utf-8"))
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError("append write made no progress")
            payload = payload[written:]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def append_regular_text(path: str | os.PathLike[str], text: str) -> None:
    with open_regular_text_append(path) as handle:
        write_locked_text(handle, text)


def _target_stat(parent_fd: int, target: Path) -> os.stat_result | None:
    try:
        info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"atomic target is not a regular file: {target}")
    return info


def write_regular_file_atomic(
    path: str | os.PathLike[str],
    writer: Callable[[BinaryIO], None],
    *,
    max_bytes: int,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
    context: str = "atomic output",
    create_only: bool = False,
) -> None:
    """Write a bounded temporary and atomically publish it in one directory."""
    limit = _require_limit(max_bytes)
    target = _absolute(path)
    ensure_directory_no_symlinks(target.parent)
    parent_fd = open_directory_no_symlinks(target.parent)
    temp_name = f".opencollab-{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    try:
        _check_parent_identity(parent_fd, expected_parent_identity, target)
        current = _target_stat(parent_fd, target)
        current_identity = None if current is None else (current.st_dev, current.st_ino)
        if expected_target_identity is not None and current_identity != expected_target_identity:
            raise OSError(f"{context} target identity changed before commit: {target}")
        exclusive = create_only or require_target_absent
        if exclusive and current_identity is not None:
            raise FileExistsError(f"{context} target appeared before commit: {target}")
        temp_fd = os.open(
            temp_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode if current is None else stat.S_IMODE(current.st_mode),
            dir_fd=parent_fd,
        )
        if current is not None:
            os.fchmod(temp_fd, stat.S_IMODE(current.st_mode))
        with os.fdopen(temp_fd, "w+b") as handle:
            temp_fd = -1
            writer(handle)
            handle.flush()
            if os.fstat(handle.fileno()).st_size > limit:
                raise ValueError(f"{context} exceeds {limit}-byte limit: {target}")
            os.fsync(handle.fileno())
        if exclusive:
            os.link(
                temp_name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=parent_fd)
        else:
            os.replace(temp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def write_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
    require_target_absent: bool = False,
) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("atomic payload must be bytes")
    limit = len(payload) if max_bytes is None else _require_limit(max_bytes)
    if len(payload) > limit:
        raise ValueError(f"atomic payload exceeds {limit}-byte limit: {path}")

    def write_payload(handle: BinaryIO) -> None:
        handle.write(payload)

    write_regular_file_atomic(
        path,
        write_payload,
        max_bytes=limit,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=expected_target_identity,
        require_target_absent=require_target_absent,
        create_only=False,
    )


def create_regular_bytes_atomic(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int | None = None,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    limit = len(payload) if max_bytes is None else max_bytes
    write_regular_file_atomic(
        path,
        lambda handle: handle.write(payload),
        max_bytes=limit,
        mode=mode,
        expected_parent_identity=expected_parent_identity,
        create_only=True,
    )


def unlink_regular_file_durable(
    path: str | os.PathLike[str],
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    expected_target_identity: tuple[int, int] | None = None,
) -> bool:
    target = _absolute(path)
    parent_fd = open_directory_no_symlinks(target.parent)
    try:
        _check_parent_identity(parent_fd, expected_parent_identity, target)
        current = _target_stat(parent_fd, target)
        current_identity = None if current is None else (current.st_dev, current.st_ino)
        if current_identity is None:
            return False
        if expected_target_identity is not None and current_identity != expected_target_identity:
            raise OwnedFileMismatchError(f"owned file identity changed: {target}")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def quarantine_unlink_owned_file(
    parent_fd: int,
    name: str,
    owned_fd: int,
    expected_identity: tuple[int, int],
    *,
    path_label: str,
) -> bool:
    """Remove a caller-owned regular file after one identity check."""
    opened = os.fstat(owned_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != expected_identity
        or (current.st_dev, current.st_ino) != expected_identity
    ):
        raise OwnedFileMismatchError(f"owned file identity changed: {path_label}")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return True


__all__ = [
    "OwnedFileMismatchError",
    "OwnedFileRetirementError",
    "append_regular_text",
    "create_regular_bytes_atomic",
    "directory_path_matches_fd",
    "directory_handle_matches_path",
    "ensure_directory_no_symlinks",
    "open_directory_no_symlinks",
    "open_regular_text_append",
    "quarantine_unlink_owned_file",
    "read_regular_bytes",
    "read_regular_text",
    "regular_handle_matches_path",
    "regular_path_identity",
    "unlink_regular_file_durable",
    "write_locked_text",
    "write_regular_bytes_atomic",
    "write_regular_file_atomic",
]
