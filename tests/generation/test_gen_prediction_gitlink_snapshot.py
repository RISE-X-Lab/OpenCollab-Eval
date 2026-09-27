from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_snapshot_container as snapshot_container


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=Snapshot Test",
        "-c",
        "user.email=snapshot@example.invalid",
        "commit",
        "-qm",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def checked_out_gitlink(tmp_path: Path) -> tuple[Path, str, str, Path]:
    module = tmp_path / "module"
    module.mkdir()
    git(module, "init", "-q")
    (module / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    oid = commit(module, "dependency base")

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "source.py").write_text("SOURCE = 1\n", encoding="utf-8")
    commit(repo, "parent base")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(module),
            "vendor/module",
        ],
        check=True,
    )
    base = commit(repo, "add dependency")
    return repo, base, oid, Path("vendor/module")


def test_snapshot_materializes_only_the_verified_gitlink_commit(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    module = repo / path
    (module / "untracked.cache").write_text("cache", encoding="utf-8")
    nested = module / "nested"
    nested.mkdir()
    git(nested, "init", "-q")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert (module / "dependency.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (module / ".git").exists()
    assert not (module / "untracked.cache").exists()
    assert not nested.exists()
    assert evidence["removed_gitlinks"] == [{"path": path.as_posix(), "old_oid": oid}]
    assert git(repo, "ls-files", "--stage", path.as_posix()).startswith(f"160000 {oid}")


def test_public_preparation_input_accepts_a_verified_materialized_gitlink(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    (repo / path / "untracked.cache").write_text("cache", encoding="utf-8")

    evidence = snapshot_container.prepare_public_input(repo, base)

    assert evidence["worktree_matches_base"] is True
    assert (repo / path / "dependency.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (repo / path / "untracked.cache").exists()
    assert git(repo, "ls-files", "--stage", path.as_posix()).startswith(f"160000 {oid}")


def test_public_preparation_can_initialize_a_gitlink_and_bind_its_bytes(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    shutil.rmtree(repo / path)
    snapshot_container.prepare_public_input(repo, base)
    module = repo / path
    module.mkdir(parents=True, exist_ok=True)
    (module / "generated.txt").write_text("public fixture\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert (module / "generated.txt").read_text(encoding="utf-8") == "public fixture\n"
    assert evidence["materialized_gitlinks"][0]["path"] == path.as_posix()
    assert evidence["materialized_gitlinks"][0]["oid"] == oid


def test_public_preparation_can_update_a_gitlink_oid_and_bind_public_bytes(tmp_path: Path) -> None:
    repo, base, _oid, path = checked_out_gitlink(tmp_path)
    new_oid = "9" * 40
    snapshot_container.prepare_public_input(repo, base)
    git(repo, "update-index", "--cacheinfo", f"160000,{new_oid},{path.as_posix()}")
    module = repo / path
    (module / "dependency.py").write_text("PUBLIC = 2\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert git(repo, "ls-files", "--stage", path.as_posix()).startswith(f"160000 {new_oid}")
    assert evidence["materialized_gitlinks"] == [
        {
            "path": path.as_posix(),
            "oid": new_oid,
            "content_sha256": evidence["materialized_gitlinks"][0]["content_sha256"],
        }
    ]


def test_public_preparation_can_bind_dirty_gitlink_content(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    snapshot_container.prepare_public_input(repo, base)
    module_file = repo / path / "dependency.py"
    module_file.write_text("PUBLIC DIRTY CONTENT\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert module_file.read_text(encoding="utf-8") == "PUBLIC DIRTY CONTENT\n"
    assert evidence["materialized_gitlinks"][0]["oid"] == oid


def test_snapshot_reconstructs_dirty_submodule_bytes_from_the_gitlink_oid(tmp_path: Path) -> None:
    repo, base, _oid, path = checked_out_gitlink(tmp_path)
    module_file = repo / path / "dependency.py"
    module_file.write_text("DIRTY = True\n", encoding="utf-8")

    snapshot_container.create_solver_snapshot(repo, base)

    assert module_file.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_snapshot_sanitizes_materialized_submodule_head_drift(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    module = repo / path
    (module / "dependency.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit(module, "future dependency")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert (module / "dependency.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git(repo, "ls-files", "--stage", path.as_posix()).startswith(f"160000 {oid}")
    assert evidence["workspace_integrity"]["outcome"] == "sanitize_then_continue"


def test_snapshot_keeps_an_uninitialized_gitlink_unmaterialized(tmp_path: Path) -> None:
    repo, base, oid, path = checked_out_gitlink(tmp_path)
    shutil.rmtree(repo / path)

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert not (repo / path).exists()
    assert evidence["removed_gitlinks"] == [{"path": path.as_posix(), "old_oid": oid}]
    assert git(repo, "status", "--porcelain", "--ignore-submodules=all") == ""


def test_snapshot_preserves_export_ignored_and_unsubstituted_base_bytes(tmp_path: Path) -> None:
    repo, _base, _oid, path = checked_out_gitlink(tmp_path)
    module = repo / path
    (module / ".gitattributes").write_text(
        "secret.txt export-ignore\nformat.txt export-subst\n",
        encoding="utf-8",
    )
    (module / "secret.txt").write_text("base secret\n", encoding="utf-8")
    (module / "format.txt").write_text("$Format:%H$\n", encoding="utf-8")
    oid = commit(module, "archive-sensitive files")
    git(repo, "add", path.as_posix())
    base = commit(repo, "update dependency")

    snapshot_container.create_solver_snapshot(repo, base)

    assert (module / "secret.txt").read_text(encoding="utf-8") == "base secret\n"
    assert (module / "format.txt").read_text(encoding="utf-8") == "$Format:%H$\n"
    assert git(repo, "ls-files", "--stage", path.as_posix()).startswith(f"160000 {oid}")


def test_snapshot_blocks_a_materialized_gitlink_with_unprovable_metadata(tmp_path: Path) -> None:
    repo, base, _oid, path = checked_out_gitlink(tmp_path)
    marker = repo / path / ".git"
    marker.write_text("gitdir: ../../../../missing-metadata\n", encoding="utf-8")

    with pytest.raises(snapshot_container.SnapshotSetupError):
        snapshot_container.create_solver_snapshot(repo, base)


def test_snapshot_accepts_a_parent_tree_with_an_absent_gitlink_object(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    oid = "2" * 40
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},vendor/missing")
    git(
        repo,
        "-c",
        "user.name=Snapshot Test",
        "-c",
        "user.email=snapshot@example.invalid",
        "commit",
        "-qm",
        "absent dependency",
    )
    base = git(repo, "rev-parse", "HEAD")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert evidence["removed_gitlinks"] == [{"path": "vendor/missing", "old_oid": oid}]
    assert not (repo / "vendor/missing").exists()
