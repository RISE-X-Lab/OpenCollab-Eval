"""Behavior checks for evaluation-owned evidence-file primitives."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import opencollab_eval.safe_files as safe_files
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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS exposes /var and /tmp compatibility aliases")
def test_macos_var_alias_is_accepted_but_final_symlink_is_not_followed() -> None:
    """The default tempfile spelling must work without weakening final-file checks."""
    actual = Path(tempfile.mkdtemp(prefix="opencollab-safe-files-"))
    try:
        canonical_var = Path("/private/var")
        canonical_actual = Path(os.path.realpath(actual))
        if not canonical_actual.is_relative_to(canonical_var):
            pytest.skip("temporary directory is not on the macOS /private/var volume")
        alias_root = Path("/var") / canonical_actual.relative_to(canonical_var)
        child = alias_root / "nested"
        ensure_directory_no_symlinks(child)
        fd = open_directory_no_symlinks(child)
        os.close(fd)
        assert child.is_dir()

        victim = actual / "victim"
        victim.write_bytes(b"keep")
        final_link = child / "output"
        final_link.symlink_to(victim)
        with pytest.raises(OSError):
            write_regular_bytes_atomic(final_link, b"must-not-follow")
        assert victim.read_bytes() == b"keep"
    finally:
        shutil.rmtree(actual, ignore_errors=True)


def test_macos_alias_validation_does_not_trust_a_user_symlink(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_files.sys, "platform", "darwin")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(safe_files, "_MACOS_SYSTEM_ALIASES", ((alias, canonical),))

    with pytest.raises(OSError, match="not a real directory"):
        ensure_directory_no_symlinks(alias / "child")


@pytest.mark.parametrize(
    ("canonical_mode", "accepted"),
    [(0o755, True), (0o1777, True), (0o777, False)],
)
def test_macos_alias_requires_root_owned_trusted_canonical_directory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_mode: int,
    accepted: bool,
) -> None:
    """A Darwin spelling is folded only for a root-owned safe target."""
    monkeypatch.setattr(safe_files.sys, "platform", "darwin")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(safe_files, "_MACOS_SYSTEM_ALIASES", ((alias, canonical),))
    original_lstat = os.lstat

    def fake_lstat(path):
        candidate = Path(path)
        if candidate == alias:
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o777)
        if candidate == canonical:
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | canonical_mode)
        return original_lstat(path)

    monkeypatch.setattr(safe_files.os, "lstat", fake_lstat)
    original_realpath = os.path.realpath
    monkeypatch.setattr(
        safe_files.os.path,
        "realpath",
        lambda path: str(canonical) if Path(path) == alias else original_realpath(path),
    )
    folded = safe_files._canonicalize_system_alias(alias / "child")
    expected = canonical / "child" if accepted else alias / "child"
    assert folded == expected
