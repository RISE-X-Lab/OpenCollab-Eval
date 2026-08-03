"""Behavior checks for evaluation-owned evidence-file primitives."""

from __future__ import annotations

import os
import stat

import pytest

from opencollab_eval.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    open_directory_no_symlinks,
    read_regular_bytes,
    write_regular_bytes_atomic,
)


def test_bounded_read_rejects_symlink_and_oversized_file(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"payload")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_regular_bytes(link, max_bytes=32)
    with pytest.raises(ValueError, match="exceeds"):
        read_regular_bytes(target, max_bytes=6)


def test_atomic_replace_preserves_full_existing_mode(tmp_path) -> None:
    path = tmp_path / "shared"
    path.write_bytes(b"old")
    path.chmod(0o666)
    previous = os.umask(0o022)
    try:
        write_regular_bytes_atomic(path, b"new")
    finally:
        os.umask(previous)
    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o666


def test_create_only_never_replaces_existing_evidence(tmp_path) -> None:
    path = tmp_path / "evidence"
    create_regular_bytes_atomic(path, b"first")
    with pytest.raises(FileExistsError):
        create_regular_bytes_atomic(path, b"second")
    assert path.read_bytes() == b"first"


def test_directory_helpers_do_not_require_component_dirfd_access(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = os.open

    def reject_relative_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise PermissionError("component dirfd access denied")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", reject_relative_open)
    target = tmp_path / "parent" / "child"
    ensure_directory_no_symlinks(target)
    fd = open_directory_no_symlinks(target)
    os.close(fd)
    assert target.is_dir()


def test_directory_helpers_reject_symlink_component(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError, match="not a real directory"):
        open_directory_no_symlinks(link)
    with pytest.raises(OSError, match="not a real directory"):
        ensure_directory_no_symlinks(link / "child")
