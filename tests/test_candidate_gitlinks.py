from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation.candidate_gitlinks import (
    capture_gitlink_manifest,
    project_gitlink_manifest,
    replay_gitlink_paths,
)
from opencollab_eval.generation.candidate_patch import (
    CandidateConstructionError,
    construct_candidate_patch,
)
from opencollab_eval.generation.candidate_patch_models import GitlinkProjection


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _materialized_gitlink(
    tmp_path: Path, *, ignored_residue: str | None = None
) -> tuple[Path, Path, str, str, Path, dict[str, object]]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "--quiet")
    _git(worktree, "config", "user.name", "Gitlink Test")
    _git(worktree, "config", "user.email", "gitlink@example.invalid")
    (worktree / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "--quiet", "-m", "base")
    oid = "1" * 40
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", oid, "vendor/module")
    _git(worktree, "commit", "--quiet", "-m", "gitlink")
    base = _git(worktree, "rev-parse", "HEAD")
    base_tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    trusted = tmp_path / "trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    module = worktree / "vendor" / "module"
    module.mkdir(parents=True)
    (module / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (module / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    if ignored_residue is not None:
        cache = module / "cache"
        cache.mkdir()
        residue = cache / "residue"
        if ignored_residue == "fifo":
            os.mkfifo(residue)
        else:
            residue.write_text("opaque\n", encoding="utf-8")
            residue.chmod(0)
    repositories = tmp_path / "gitlink-repositories"
    manifest = capture_gitlink_manifest(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        base_tree=base_tree,
        baseline_sha256="a" * 64,
        repository_directory=repositories,
    )
    return worktree, trusted, base, oid, repositories, manifest


def test_gitlink_projection_preserves_unchanged_content_and_ignored_residue(tmp_path: Path) -> None:
    worktree, _trusted, _base, oid, repositories, manifest = _materialized_gitlink(tmp_path)
    cache = worktree / "vendor" / "module" / "cache"
    cache.mkdir()
    (cache / "result.bin").write_bytes(b"ignored")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=_trusted,
        repository_directory=repositories,
    )

    item = projected["gitlinks"][0]
    assert item["oid"] == oid
    assert item["action"] == "preserve"
    assert item["baseline_digest"] == item["current_digest"]


def test_gitlink_projection_replaces_visible_content_change(tmp_path: Path) -> None:
    worktree, _trusted, _base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    (worktree / "vendor" / "module" / "dependency.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=_trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "replacement"


def test_gitlink_replacement_preserves_nested_baseline_ignore_policy(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    worktree, trusted, base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    module = worktree / "vendor" / "module"
    (module / "dependency.py").write_text("VALUE = 2\n", encoding="utf-8")
    cache = module / "cache"
    cache.mkdir()
    os.mkfifo(cache / "pipe")
    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )
    item = projected["gitlinks"][0]
    projection = GitlinkProjection(
        item["path"],
        item["oid"],
        item["action"],
        item["baseline_digest"],
        item["current_digest"],
        tuple(item["ignored_paths"]),
    )

    candidate = construct_candidate_patch(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        baseline_sha256="a" * 64,
        max_patch_bytes=8 * 1024 * 1024,
        gitlinks=(projection,),
    )

    assert "dependency.py" in candidate.patch
    assert "cache/pipe" not in candidate.patch


def test_materialized_gitlink_cannot_hide_change_with_solver_ignore(tmp_path: Path) -> None:
    worktree, trusted, _base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    module = worktree / "vendor" / "module"
    (module / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (module / "answer.py").write_text("ANSWER = True\n", encoding="utf-8")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "replacement"


def test_gitlink_projection_deletes_visible_materialized_component(tmp_path: Path) -> None:
    worktree, _trusted, _base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    shutil.rmtree(worktree / "vendor" / "module")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=_trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "delete"


def test_gitlink_projection_rejects_repository_path_escape(tmp_path: Path) -> None:
    worktree, _trusted, _base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    forged = copy.deepcopy(manifest)
    forged["gitlinks"][0]["baseline_repository"] = "../outside"

    with pytest.raises(CandidateConstructionError, match="baseline entry is invalid"):
        project_gitlink_manifest(
            manifest=forged,
            worktree=worktree,
            git_dir=_trusted,
            repository_directory=repositories,
        )


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_gitlink_capture_rejects_non_directory_baseline(tmp_path: Path, kind: str) -> None:
    worktree, trusted, base, _oid, repositories, _manifest = _materialized_gitlink(tmp_path)
    module = worktree / "vendor" / "module"
    shutil.rmtree(module)
    if kind == "file":
        module.write_text("ordinary file\n", encoding="utf-8")
    else:
        module.symlink_to("dependency.py")
    shutil.rmtree(repositories)

    with pytest.raises(CandidateConstructionError, match="baseline is not a directory"):
        capture_gitlink_manifest(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            base_tree=_git(worktree, "rev-parse", "HEAD^{tree}"),
            baseline_sha256="a" * 64,
            repository_directory=repositories,
        )


@pytest.mark.parametrize("residue", ["fifo", "unreadable"])
def test_gitlink_capture_never_opens_ignored_special_residue(
    tmp_path: Path, residue: str
) -> None:
    if residue == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    worktree, trusted, _base, _oid, repositories, manifest = _materialized_gitlink(
        tmp_path, ignored_residue=residue
    )
    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "preserve"


def test_unmaterialized_gitlink_preserves_only_ignored_residue(tmp_path: Path) -> None:
    worktree = tmp_path / "unmaterialized"
    worktree.mkdir()
    _git(worktree, "init", "--quiet")
    _git(worktree, "config", "user.name", "Gitlink Test")
    _git(worktree, "config", "user.email", "gitlink@example.invalid")
    (worktree / ".gitignore").write_text("vendor/module/runtime.cache\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "--quiet", "-m", "base")
    oid = "1" * 40
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", oid, "vendor/module")
    _git(worktree, "commit", "--quiet", "-m", "gitlink")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "unmaterialized.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    repositories = tmp_path / "unmaterialized-repositories"
    manifest = capture_gitlink_manifest(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        base_tree=_git(worktree, "rev-parse", "HEAD^{tree}"),
        baseline_sha256="a" * 64,
        repository_directory=repositories,
    )
    residue = worktree / "vendor" / "module" / "runtime.cache"
    residue.parent.mkdir(parents=True)
    residue.write_text("ignored\n", encoding="utf-8")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "preserve"
    assert replay_gitlink_paths(projected) == ()


def test_gitlink_capture_accepts_large_trusted_tree(tmp_path: Path) -> None:
    git_dir = tmp_path / "large.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(git_dir))
    blob = subprocess.run(
        ["git", f"--git-dir={git_dir}", "hash-object", "-w", "--stdin"],
        input=b"content\n",
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    gitlink_oid = "1" * 40
    vendor_tree = subprocess.run(
        ["git", f"--git-dir={git_dir}", "mktree", "-z", "--missing"],
        input=f"160000 commit {gitlink_oid}\tmodule\0".encode(),
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    entries = b"".join(
        f"100644 blob {blob}\tfile-{index:05d}-{'x' * 48}\0".encode()
        for index in range(10_000)
    ) + f"040000 tree {vendor_tree}\tvendor\0".encode()
    tree = subprocess.run(
        ["git", f"--git-dir={git_dir}", "mktree", "-z", "--missing"],
        input=entries,
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    census = subprocess.run(
        ["git", f"--git-dir={git_dir}", "ls-tree", "-rz", "--full-tree", tree],
        capture_output=True,
        check=True,
    ).stdout
    assert len(census) > 1024 * 1024
    worktree = tmp_path / "large-worktree"
    worktree.mkdir()

    manifest = capture_gitlink_manifest(
        git_dir=git_dir,
        worktree=worktree,
        base=tree,
        base_tree=tree,
        baseline_sha256="a" * 64,
        repository_directory=tmp_path / "large-repositories",
    )

    assert manifest["gitlinks"] == [
        {
            "path": "vendor/module",
            "oid": gitlink_oid,
            "baseline_digest": None,
            "baseline_repository": None,
        }
    ]


def test_unmaterialized_gitlink_uses_baseline_ignore_rules(tmp_path: Path) -> None:
    worktree, trusted, _base, _oid, repositories, manifest = _materialized_gitlink(tmp_path)
    module = worktree / "vendor" / "module"
    shutil.rmtree(module)
    manifest["gitlinks"][0]["baseline_digest"] = None
    manifest["gitlinks"][0]["baseline_repository"] = None
    shutil.rmtree(repositories)
    module.mkdir(parents=True)
    (worktree / ".gitignore").write_text("vendor/module/**\n", encoding="utf-8")
    (module / ".gitignore").write_text("*\n", encoding="utf-8")
    (module / "answer.py").write_text("ANSWER = True\n", encoding="utf-8")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "replacement"


def test_gitlink_visibility_treats_magic_prefix_as_literal_path(tmp_path: Path) -> None:
    worktree = tmp_path / "literal"
    worktree.mkdir()
    _git(worktree, "init", "--quiet")
    _git(worktree, "config", "user.name", "Gitlink Test")
    _git(worktree, "config", "user.email", "gitlink@example.invalid")
    (worktree / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(worktree, "add", "source.py")
    _git(worktree, "commit", "--quiet", "-m", "base")
    path = ":(exclude)module"
    oid = "1" * 40
    _git(
        worktree,
        "--literal-pathspecs",
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        oid,
        path,
    )
    _git(worktree, "commit", "--quiet", "-m", "gitlink")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "literal.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    repositories = tmp_path / "literal-repositories"
    manifest = capture_gitlink_manifest(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        base_tree=_git(worktree, "rev-parse", "HEAD^{tree}"),
        baseline_sha256="a" * 64,
        repository_directory=repositories,
    )
    replacement = worktree / path
    replacement.mkdir()
    (replacement / "visible.py").write_text("VALUE = 2\n", encoding="utf-8")

    projected = project_gitlink_manifest(
        manifest=manifest,
        worktree=worktree,
        git_dir=trusted,
        repository_directory=repositories,
    )

    assert projected["gitlinks"][0]["action"] == "replacement"
