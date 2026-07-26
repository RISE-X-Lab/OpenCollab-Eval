"""Small filesystem helpers shared by host and container snapshot setup."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path


def anonymous_commit_oid(base_tree: str) -> str:
    """Return the deterministic solver-snapshot commit identity for one tree."""
    algorithm = hashlib.sha1 if len(base_tree) == 40 else hashlib.sha256
    if len(base_tree) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in base_tree
    ):
        raise ValueError("base tree must be a lowercase full object id")
    content = (
        f"tree {base_tree}\n"
        "author OpenCollab Solver Snapshot <solver-snapshot@invalid> 946684800 +0000\n"
        "committer OpenCollab Solver Snapshot <solver-snapshot@invalid> 946684800 +0000\n\n"
        "solver snapshot\n"
    ).encode("ascii")
    return algorithm(f"commit {len(content)}\0".encode("ascii") + content).hexdigest()


def replace_worktree_contents(original: Path, prepared: Path) -> None:
    """Install prepared files while retaining the task copy's object store."""
    for child in original.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
    for child in list(prepared.iterdir()):
        child.rename(original / child.name)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def sanitize_preparation_repository(repo_root: Path, object_format: str) -> None:
    """Remove image-local Git behavior while retaining local object reads."""
    git_dir = repo_root / ".git"
    config_path = git_dir / "config"
    _remove_path(config_path)
    repository_format = "1" if object_format == "sha256" else "0"
    config = (
        "[core]\n"
        f"\trepositoryformatversion = {repository_format}\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = false\n"
    )
    if object_format == "sha256":
        config += "[extensions]\n\tobjectFormat = sha256\n"
    config_path.write_text(config, encoding="utf-8")
    _remove_path(git_dir / "config.worktree")
    for relative in ("hooks", "modules", "info"):
        path = git_dir / relative
        _remove_path(path)
        path.mkdir(parents=True)


def copy_public_preparation(source: Path, destination: Path) -> list[str]:
    """Copy public preparation output without nested repository metadata."""
    symlinks: list[str] = []

    def ignore(directory: str, names: list[str]) -> set[str]:
        del directory
        return {".git"} & set(names)

    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True, ignore=ignore)
    for current, directories, files in os.walk(destination, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            entry = current_path / name
            if entry.is_symlink():
                symlinks.append(entry.relative_to(destination).as_posix())
    return symlinks


def workspace_sha256(root: Path) -> str:
    """Hash visible entries, modes, empty directories, and symlink targets."""
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        current_path = Path(current)
        for name in [*directories, *files]:
            entry = current_path / name
            relative = entry.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")
            metadata = entry.lstat()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            if entry.is_symlink():
                target = os.fsencode(os.readlink(entry))
                digest.update(b"L" + len(target).to_bytes(8, "big") + target)
            elif entry.is_dir():
                digest.update(b"D" + stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
            elif entry.is_file():
                digest.update(b"F" + stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
                digest.update(metadata.st_size.to_bytes(8, "big"))
                with entry.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise ValueError("workspace contains an unsupported special file")
    return digest.hexdigest()


__all__ = [
    "anonymous_commit_oid",
    "copy_public_preparation",
    "replace_worktree_contents",
    "sanitize_preparation_repository",
    "workspace_sha256",
]
