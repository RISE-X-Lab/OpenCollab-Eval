"""Behavior checks for evaluation-owned evidence-file primitives."""

from __future__ import annotations

import os
import stat

import pytest

from opencollab_eval.safe_files import (
    create_regular_bytes_atomic,
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
