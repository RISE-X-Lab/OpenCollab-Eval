from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from opencollab_eval.generation import workspace_archive as patcher


@pytest.mark.parametrize("name", ["../escape", "/absolute"])
def test_archive_member_rejects_unsafe_entries(tmp_path: Path, name: str) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    payload.seek(0)
    with tarfile.open(fileobj=payload, mode="r:") as archive:
        member = archive.next()
        assert member is not None
        with pytest.raises(RuntimeError):
            patcher._extract_member(archive, member, tmp_path / "out", extracted_bytes=0)


def test_archive_materializes_safe_hardlink_without_copying_payload(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        source = tarfile.TarInfo("source.txt")
        source.size = 4
        archive.addfile(source, io.BytesIO(b"data"))
        link = tarfile.TarInfo("copy.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "source.txt"
        archive.addfile(link)
    payload.seek(0)
    pending: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    extracted = 0
    with tarfile.open(fileobj=payload, mode="r:") as archive:
        for member in archive:
            extracted = patcher._extract_member(
                archive, member, root, extracted_bytes=extracted, pending_hardlinks=pending
            )
    patcher._materialize_hardlinks(root, pending)
    assert extracted == 4
    assert (root / "copy.txt").read_bytes() == b"data"
    assert (root / "copy.txt").stat().st_ino == (root / "source.txt").stat().st_ino


def test_archive_preserves_special_entry_for_candidate_policy(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    root = tmp_path / "out"
    root.mkdir()
    member = tarfile.TarInfo("cache/pipe")
    member.type = tarfile.FIFOTYPE
    archive = tarfile.open(fileobj=io.BytesIO(), mode="w")
    try:
        assert patcher._extract_member(archive, member, root, extracted_bytes=0) == 0
    finally:
        archive.close()
    assert stat.S_ISFIFO((root / "cache" / "pipe").lstat().st_mode)


def test_archive_member_records_an_outward_symlink_without_reading_it(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    member = tarfile.TarInfo("link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../escape"
    archive = tarfile.open(fileobj=io.BytesIO(), mode="w")
    try:
        assert patcher._extract_member(archive, member, root, extracted_bytes=0) == 0
    finally:
        archive.close()
    assert (root / "link").is_symlink()
    assert (root / "link").readlink().as_posix() == "../../escape"


def test_archive_member_rejects_duplicate_path(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "same").write_text("first", encoding="utf-8")
    member = tarfile.TarInfo("same")
    member.size = 1
    archive = tarfile.open(fileobj=io.BytesIO(), mode="w")
    try:
        with pytest.raises(RuntimeError, match="duplicate"):
            patcher._extract_member(archive, member, root, extracted_bytes=0)
    finally:
        archive.close()


def test_tar_extraction_preserves_directory_and_file_modes(tmp_path: Path) -> None:
    def extract(root: Path, directory_mode: int, file_mode: int) -> tuple[int, int]:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            directory = tarfile.TarInfo("package")
            directory.type = tarfile.DIRTYPE
            directory.mode = directory_mode
            archive.addfile(directory)
            file = tarfile.TarInfo("package/module.py")
            file.mode = file_mode
            file.size = 1
            archive.addfile(file, io.BytesIO(b"x"))
        payload.seek(0)
        modes: dict[tuple[str, ...], int] = {}
        extracted = 0
        with tarfile.open(fileobj=payload, mode="r:") as archive:
            for member in archive:
                extracted = patcher._extract_member(
                    archive, member, root, extracted_bytes=extracted, directory_modes=modes
                )
        patcher._restore_directory_modes(root, modes)
        return (
            (root / "package").stat().st_mode & 0o777,
            (root / "package" / "module.py").stat().st_mode & 0o777,
        )

    baseline_root = tmp_path / "baseline-tar"
    baseline_root.mkdir()
    changed_root = tmp_path / "changed-tar"
    changed_root.mkdir()
    assert extract(baseline_root, 0o755, 0o644) == (0o755, 0o644)
    assert extract(changed_root, 0o700, 0o444) == (0o700, 0o444)
