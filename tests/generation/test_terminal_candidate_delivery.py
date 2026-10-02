"""Filesystem delivery evidence for the retained Terminal candidate backend."""
from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from opencollab_eval.generation.terminal_container_backend import file_snapshot, state_diff


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({}, {"/app/run.py": (0o644, b"print(1)\n", "file")}, "+print(1)"),
        ({"/app/run.py": (0o644, b"old\n", "file")}, {}, "deleted file mode"),
        (
            {"/app/run.sh": (0o644, b"true\n", "file")},
            {"/app/run.sh": (0o755, b"true\n", "file")},
            "new mode 100755",
        ),
        ({}, {"/app/current": (0o777, b"release", "symlink")}, "new file mode 120000"),
        ({}, {"/app/data": (0o755, b"", "directory")}, "new file mode 040000"),
    ],
)
def test_delivery_diff_preserves_content_and_file_kind(before, after, expected):
    assert expected in state_diff(before, after)


def test_binary_delivery_diff_can_reconstruct_exact_payload(tmp_path):
    before = b"original\x00payload"
    after = b"replacement\x00payload\xff"
    diff = state_diff(
        {"/artifact.bin": (0o644, before, "file")},
        {"/artifact.bin": (0o644, after, "file")},
    )
    (tmp_path / "artifact.bin").write_bytes(before)
    result = subprocess.run(
        ["git", "apply", "--binary", "-"], input=diff, text=True, cwd=tmp_path, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "artifact.bin").read_bytes() == after


@pytest.mark.parametrize("member_name", ["app/result.txt", "../../outside"])
def test_snapshot_retains_bytes_and_rejects_parent_traversal(tmp_path, monkeypatch, member_name):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = 6
        member.mode = 0o640
        archive.addfile(member, io.BytesIO(b"answer"))

    def docker(*args, **kwargs):
        return data.getvalue() if "tar" in args else b"/app/result.txt\0"

    monkeypatch.setattr("opencollab_eval.generation.terminal_container_backend.docker", docker)
    if member_name.startswith(".."):
        with pytest.raises(RuntimeError, match="unsafe filesystem"):
            file_snapshot("container", ["/app/result.txt"], tmp_path / "snapshot.tar")
    else:
        result = file_snapshot("container", ["/app/result.txt"], tmp_path / "snapshot.tar")
        assert result == {"/app/result.txt": (0o640, b"answer", "file")}

