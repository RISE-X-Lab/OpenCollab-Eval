"""Bounded extraction of a frozen solver container workspace."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

from opencollab_eval.engine.swe_generation_proof import (
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_EXTRACTED_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
)

from .gen_prediction_config import _workspace_archive_timeout_from_env
from .gen_prediction_constants import DOCKER_WORKDIR


class _BoundedHashReader:
    def __init__(self, raw, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        allowed = self.limit - self.count
        if allowed < 0:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        request = allowed + 1 if size < 0 else min(size, allowed + 1)
        data = self.raw.read(request)
        if not data:
            return b""
        self.count += len(data)
        if self.count > self.limit:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        self.digest.update(data)
        return data


class _WorkspaceArchiveTruncated(RuntimeError):
    """The Docker archive stream ended before one complete workspace arrived."""


class _WorkspaceArchiveTimeout(RuntimeError):
    """The bounded workspace archive operation exceeded its wall-clock budget."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        elapsed_seconds: float,
        archive_bytes: int,
        archive_entries: int,
        extracted_bytes: int,
        docker_stderr: str,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        self.archive_bytes = archive_bytes
        self.archive_entries = archive_entries
        self.extracted_bytes = extracted_bytes
        self.docker_stderr = docker_stderr
        detail = (
            "container workspace archive copy timed out "
            f"after {elapsed_seconds:.3f}s "
            f"(limit={timeout_seconds:g}s, archive_bytes={archive_bytes}, "
            f"archive_entries={archive_entries}, extracted_bytes={extracted_bytes})"
        )
        if docker_stderr:
            detail += f": docker stderr: {docker_stderr}"
        super().__init__(detail)


_MAX_DOCKER_STDERR_BYTES = 16 * 1024


def _bounded_docker_stderr(stderr_file) -> str:
    stderr_file.flush()
    size = stderr_file.seek(0, os.SEEK_END)
    offset = max(0, size - _MAX_DOCKER_STDERR_BYTES)
    stderr_file.seek(offset)
    raw = stderr_file.read(_MAX_DOCKER_STDERR_BYTES)
    detail = raw.decode("utf-8", errors="replace").strip()
    return ("..." if offset else "") + detail


def _member_parts(name: str) -> tuple[str, ...]:
    if "\x00" in name:
        raise RuntimeError("container workspace archive contains a NUL path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise RuntimeError("container workspace archive contains an absolute path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return ()
    if any(part == ".." for part in parts):
        raise RuntimeError("container workspace archive escapes the extraction root")
    return parts


def _safe_parent(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise RuntimeError("container workspace archive traverses a non-directory parent")
    return root.joinpath(*parts)


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root: Path,
    *,
    extracted_bytes: int,
    directory_modes: dict[tuple[str, ...], int] | None = None,
    pending_hardlinks: list[tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
) -> int:
    parts = _member_parts(member.name)
    if not parts:
        if member.isdir():
            return extracted_bytes
        raise RuntimeError("container workspace archive has a non-directory root entry")
    destination = _safe_parent(root, parts)
    if member.isdir():
        if directory_modes is not None:
            directory_modes[parts] = member.mode & 0o777
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            destination.mkdir()
        else:
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise RuntimeError("container workspace archive redefines a path as a directory")
        return extracted_bytes
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("container workspace archive contains a duplicate path")
    if member.issym():
        if "\x00" in member.linkname:
            raise RuntimeError("container workspace archive contains an invalid symlink")
        os.symlink(member.linkname, destination)
        return extracted_bytes
    if member.islnk():
        target_parts = _member_parts(member.linkname)
        if not target_parts or pending_hardlinks is None:
            raise RuntimeError("container workspace archive contains an invalid hard link")
        pending_hardlinks.append((parts, target_parts))
        return extracted_bytes
    if not member.isfile():
        os.mkfifo(destination, member.mode & 0o777)
        return extracted_bytes
    if member.size < 0 or member.size > MAX_WORKSPACE_FILE_BYTES:
        raise RuntimeError("container workspace archive contains an oversized file")
    total = extracted_bytes + member.size
    if total > MAX_WORKSPACE_EXTRACTED_BYTES:
        raise RuntimeError("container workspace extraction exceeded its byte limit")
    source = archive.extractfile(member)
    if source is None:
        raise RuntimeError("container workspace archive file payload is missing")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, member.mode & 0o777 or 0o600)
    written = 0
    try:
        while written < member.size:
            chunk = source.read(min(1024 * 1024, member.size - written))
            if not chunk:
                raise _WorkspaceArchiveTruncated(
                    "container workspace archive file payload is truncated"
                )
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            written += len(chunk)
        os.fchmod(fd, member.mode & 0o777)
        os.fsync(fd)
    finally:
        os.close(fd)
    return total


def _materialize_hardlinks(
    root: Path,
    pending: list[tuple[tuple[str, ...], tuple[str, ...]]],
) -> None:
    remaining = list(pending)
    while remaining:
        deferred: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        progress = False
        for destination_parts, target_parts in remaining:
            destination = _safe_parent(root, destination_parts)
            target = root.joinpath(*target_parts)
            if not os.path.lexists(target):
                deferred.append((destination_parts, target_parts))
                continue
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("container workspace archive hard link target is unsafe")
            os.link(target, destination, follow_symlinks=False)
            progress = True
        if deferred and not progress:
            raise RuntimeError("container workspace archive hard link target is missing")
        remaining = deferred


def _restore_directory_modes(root: Path, directory_modes: dict[tuple[str, ...], int]) -> None:
    for parts, mode in sorted(directory_modes.items(), key=lambda item: len(item[0]), reverse=True):
        if not parts:
            continue
        directory = root.joinpath(*parts)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("container workspace archive directory changed type during extraction")
        os.chmod(directory, mode)


def _copy_workspace_archive(container_id: str, root: Path) -> tuple[str, int, int, int]:
    command = ["docker", "cp", f"{container_id}:{DOCKER_WORKDIR}/.", "-"]
    timeout_seconds = _workspace_archive_timeout_from_env()
    started_at = time.monotonic()
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file)
        if process.stdout is None:
            process.kill()
            raise RuntimeError("docker cp did not expose its archive stream")
        timed_out = threading.Event()

        def timeout_error() -> _WorkspaceArchiveTimeout:
            return _WorkspaceArchiveTimeout(
                timeout_seconds=timeout_seconds,
                elapsed_seconds=time.monotonic() - started_at,
                archive_bytes=reader.count,
                archive_entries=entries,
                extracted_bytes=extracted,
                docker_stderr=_bounded_docker_stderr(stderr_file),
            )

        def kill_on_timeout() -> None:
            timed_out.set()
            if process.poll() is not None:
                return
            try:
                process.kill()
            except ProcessLookupError:
                pass

        def stop_process() -> None:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()

        timer = threading.Timer(timeout_seconds, kill_on_timeout)
        reader = _BoundedHashReader(process.stdout, MAX_WORKSPACE_ARCHIVE_BYTES)
        entries = 0
        extracted = 0
        directory_modes: dict[tuple[str, ...], int] = {}
        pending_hardlinks: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        timer.start()
        try:
            with tarfile.open(fileobj=reader, mode="r|*") as archive:
                for member in archive:
                    entries += 1
                    if entries > MAX_WORKSPACE_ARCHIVE_ENTRIES:
                        raise RuntimeError("container workspace archive exceeded its entry limit")
                    extracted = _extract_member(
                        archive,
                        member,
                        root,
                        extracted_bytes=extracted,
                        directory_modes=directory_modes,
                        pending_hardlinks=pending_hardlinks,
                    )
            _materialize_hardlinks(root, pending_hardlinks)
            _restore_directory_modes(root, directory_modes)
            while reader.read(1024 * 1024):
                pass
            returncode = process.wait(timeout=5)
            if timed_out.is_set():
                raise timeout_error()
            if returncode != 0:
                stderr = _bounded_docker_stderr(stderr_file)
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(
                    f"docker cp workspace archive failed with exit {returncode}{detail}"
                )
            return reader.digest.hexdigest(), reader.count, entries, extracted
        except tarfile.ReadError as exc:
            stop_process()
            if timed_out.is_set():
                raise timeout_error() from exc
            raise _WorkspaceArchiveTruncated(
                f"container workspace archive stream is truncated: {exc}"
            ) from exc
        except BaseException as exc:
            stop_process()
            if timed_out.is_set() and not isinstance(exc, _WorkspaceArchiveTimeout):
                raise timeout_error() from exc
            raise
        finally:
            timer.cancel()
            process.stdout.close()
