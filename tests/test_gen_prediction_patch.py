from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from generation_proof_test_support import current_inline_generation_schema_fields

from opencollab_eval.engine.swe_generation_proof import (
    current_generation_proof_valid,
    current_generation_summary_proof_valid,
)
from opencollab_eval.generation import gen_prediction_patch as patcher
from opencollab_eval.generation import gen_prediction_patch_git as patch_git
from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _baseline(
    tmp_path: Path,
    *,
    object_format: str = "sha1",
) -> tuple[patcher.TrustedPatchBaseline, Path]:
    repo = tmp_path / "base"
    repo.mkdir()
    init_args = ["init", "--quiet"]
    if object_format == "sha256":
        init_args.append("--object-format=sha256")
    _git(repo, *init_args)
    _git(repo, "config", "user.name", "Harness Test")
    _git(repo, "config", "user.email", "harness@example.invalid")
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    bare = tmp_path / "trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(bare))
    snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    return (
        patcher.TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=None,
            git_dir=bare,
            archive_sha256="a" * 64,
            archive_bytes=100,
            archive_entries=2,
            extracted_bytes=50,
        ),
        repo,
    )


def _worktree(repo: Path, destination: Path) -> Path:
    shutil.copytree(repo, destination, symlinks=True)
    return destination


def test_host_extractor_ignores_solver_git_config_head_index_and_shell(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "worktree")
    (worktree / "source.txt").write_text("fixed\n", encoding="utf-8")
    (worktree / ".git" / "config").write_text(
        "[diff]\n\texternal = /bin/false\n",
        encoding="utf-8",
    )
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/forged\n", encoding="utf-8")
    (worktree / ".bash_profile").write_text("exit 99\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "-base" in extracted
    assert "+fixed" in extracted
    assert ".bash_profile" in extracted


def test_host_extractor_captures_solver_commits_against_fixed_base(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "committed")
    (worktree / "source.txt").write_text("committed repair\n", encoding="utf-8")
    _git(worktree, "add", "source.txt")
    _git(worktree, "commit", "--quiet", "-m", "solver commit")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "+committed repair" in extracted


def test_host_extractor_supports_sha256_baselines(tmp_path: Path) -> None:
    try:
        baseline, repo = _baseline(tmp_path, object_format="sha256")
    except subprocess.CalledProcessError:
        pytest.skip("host Git does not support SHA-256 repositories")
    worktree = _worktree(repo, tmp_path / "sha256-worktree")
    (worktree / "source.txt").write_text("sha256 repair\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert len(baseline.snapshot.anonymous_head) == 64
    assert "+sha256 repair" in extracted


def test_host_extractor_does_not_trust_final_ignore_rules(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "ignored")
    (worktree / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (worktree / "repair.py").write_text("fixed = True\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "repair.py" in extracted
    assert "+*.py" in extracted


def test_host_extractor_flattens_copied_nested_repository_metadata(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "nested-repository")
    nested = worktree / "copied-source"
    nested.mkdir()
    _git(nested, "init", "--quiet")
    _git(nested, "config", "user.name", "Nested")
    _git(nested, "config", "user.email", "nested@example.invalid")
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(nested, "add", "module.py")
    _git(nested, "commit", "--quiet", "-m", "nested")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "copied-source/module.py" in extracted
    assert "new file mode 160000" not in extracted


def test_host_extractor_ignores_a_gitlink_added_only_to_solver_metadata(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "added-gitlink")
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", "9" * 40, "vendor/new")

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_a_new_empty_directory(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "empty-added")
    (worktree / "empty").mkdir()

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_a_deleted_baseline_empty_directory(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    (repo / "empty").mkdir()
    worktree = _worktree(repo, tmp_path / "empty-deleted")
    (worktree / "empty").rmdir()

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_a_non_executable_file_mode_change(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "file-mode")
    (worktree / "source.txt").chmod(0o444)

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_a_directory_mode_change(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    directory = repo / "package"
    directory.mkdir()
    (directory / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "add package")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    worktree = _worktree(repo, tmp_path / "directory-mode")
    (worktree / "package").chmod(0o700)

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_preserves_absent_baseline_gitlink(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    gitlink = "integration"
    oid = "1" * len(baseline.snapshot.anonymous_head)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", oid, gitlink)
    _git(repo, "commit", "--quiet", "-m", "add gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, oid),))
    baseline = patcher.TrustedPatchBaseline(
        snapshot=snapshot,
        temporary_directory=None,
        git_dir=baseline.git_dir,
        archive_sha256=baseline.archive_sha256,
        archive_bytes=baseline.archive_bytes,
        archive_entries=baseline.archive_entries,
        extracted_bytes=baseline.extracted_bytes,
        gitlink_worktrees=((gitlink, None),),
    )
    worktree = _worktree(repo, tmp_path / "gitlink-worktree")

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_invisible_gitlink_index_deletion(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    gitlink = "integration"
    oid = "1" * len(baseline.snapshot.anonymous_head)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", oid, gitlink)
    _git(repo, "commit", "--quiet", "-m", "add gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, oid),))
    baseline.gitlink_worktrees = ((gitlink, None),)
    worktree = _worktree(repo, tmp_path / "deleted-gitlink-worktree")
    _git(worktree, "rm", "--cached", "--quiet", gitlink)

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert extracted == ""


@pytest.mark.parametrize("replacement", ("file", "directory"))
def test_host_extractor_preserves_gitlink_replacement_with_regular_files(
    tmp_path: Path,
    replacement: str,
) -> None:
    baseline, repo = _baseline(tmp_path)
    gitlink = "vendor/module"
    oid = "1" * len(baseline.snapshot.anonymous_head)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", oid, gitlink)
    _git(repo, "commit", "--quiet", "-m", "add gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, oid),))
    baseline.gitlink_worktrees = ((gitlink, None),)
    worktree = _worktree(repo, tmp_path / f"gitlink-to-{replacement}")
    _git(worktree, "rm", "--cached", "--quiet", gitlink)
    target = worktree / gitlink
    if replacement == "directory":
        target.mkdir(parents=True)
        (target / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(worktree, "add", f"{gitlink}/a.py")
    else:
        target.parent.mkdir(parents=True)
        target.write_text("ordinary file\n", encoding="utf-8")
        _git(worktree, "add", gitlink)

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "deleted file mode 160000" in extracted
    assert "new file mode 100644" in extracted
    assert ("a.py" if replacement == "directory" else gitlink) in extracted


def test_host_extractor_treats_magic_gitlink_path_as_literal(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    gitlink = ":(exclude)vendor/module"
    oid = "1" * len(baseline.snapshot.anonymous_head)
    _git(
        repo,
        "--literal-pathspecs",
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        oid,
        gitlink,
    )
    _git(repo, "commit", "--quiet", "-m", "add magic gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, oid),))
    baseline.gitlink_worktrees = ((gitlink, None),)
    worktree = _worktree(repo, tmp_path / "magic-gitlink")
    target = worktree / gitlink
    target.mkdir(parents=True)
    (target / "visible.py").write_text("VALUE = 1\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "deleted file mode 160000" in extracted
    assert "visible.py" in extracted


def test_host_extractor_ignores_untrusted_private_gitlink_oid_update(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    gitlink = "integration"
    old_oid = "1" * len(baseline.snapshot.anonymous_head)
    new_oid = "2" * len(baseline.snapshot.anonymous_head)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", old_oid, gitlink)
    _git(repo, "commit", "--quiet", "-m", "add gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, old_oid),))
    baseline.gitlink_worktrees = ((gitlink, None),)
    worktree = _worktree(repo, tmp_path / "updated-gitlink")
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", new_oid, gitlink)

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert extracted == ""


@pytest.mark.parametrize("target", ("/eval_input/test_patch", "../../eval_input/f2p.targets.json"))
def test_host_extractor_rejects_new_outward_symlink(tmp_path: Path, target: str) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "outward-link")
    (worktree / "oracle").symlink_to(target)

    with pytest.raises(RuntimeError, match="escapes the worktree"):
        patcher._extract_patch_from_copy(worktree, baseline)


@pytest.mark.parametrize("target", ("/eval_input/test_patch", "../../eval_input/f2p.targets.json"))
def test_host_extractor_rejects_tracked_file_changed_to_outward_symlink(
    tmp_path: Path,
    target: str,
) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "file-to-outward-link")
    (worktree / "source.txt").unlink()
    (worktree / "source.txt").symlink_to(target)

    with pytest.raises(RuntimeError, match="escapes the worktree"):
        patcher._extract_patch_from_copy(worktree, baseline)


def test_host_extractor_rejects_modified_baseline_link_that_turns_outward(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    (repo / "optional").symlink_to("missing-local-target")
    _git(repo, "add", "optional")
    _git(repo, "commit", "--quiet", "-m", "add broken link")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    worktree = _worktree(repo, tmp_path / "modified-link")
    (worktree / "optional").unlink()
    (worktree / "optional").symlink_to("/eval_input/test_patch")

    with pytest.raises(RuntimeError, match="escapes the worktree"):
        patcher._extract_patch_from_copy(worktree, baseline)


def test_host_extractor_allows_unchanged_broken_baseline_link(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    (repo / "optional").symlink_to("missing-local-target")
    _git(repo, "add", "optional")
    _git(repo, "commit", "--quiet", "-m", "add broken link")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    worktree = _worktree(repo, tmp_path / "unchanged-link")

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_rejects_renamed_broken_outward_baseline_link(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    (repo / "optional").symlink_to("../../missing-runtime")
    _git(repo, "add", "optional")
    _git(repo, "commit", "--quiet", "-m", "add broken outward link")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    baseline.snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    worktree = _worktree(repo, tmp_path / "renamed-link")
    _git(worktree, "mv", "optional", "renamed")

    with pytest.raises(RuntimeError, match="escapes the worktree"):
        patcher._extract_patch_from_copy(worktree, baseline)


def _materialized_gitlink_baseline(
    tmp_path: Path,
    *,
    tracked_parent_link: bool = False,
    gitlink: str = "vendor/module",
) -> tuple[patcher.TrustedPatchBaseline, Path, str]:
    baseline, repo = _baseline(tmp_path)
    oid = "2" * len(baseline.snapshot.anonymous_head)
    module = repo / gitlink
    module.mkdir(parents=True)
    (module / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    (module / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    if tracked_parent_link:
        (module / "source-link").symlink_to("../../source.txt")
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", oid, gitlink)
    _git(repo, "commit", "--quiet", "-m", "add materialized gitlink")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    shutil.rmtree(baseline.git_dir)
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(baseline.git_dir))
    snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1, ((gitlink, oid),))
    repositories = patch_git.prepare_gitlink_state_repositories(
        repo,
        snapshot,
        tmp_path / "gitlink-state",
        "git",
        env=dict(os.environ),
        timeout=30,
    )
    inventory = tuple(
        (
            path,
            patch_git.gitlink_repository_digest(
                repository, "git", env=dict(os.environ), timeout=30
            ),
        )
        for path, repository in repositories
    )
    return (
        patcher.TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=None,
            git_dir=baseline.git_dir,
            archive_sha256=baseline.archive_sha256,
            archive_bytes=baseline.archive_bytes,
            archive_entries=baseline.archive_entries,
            extracted_bytes=baseline.extracted_bytes,
            gitlink_worktrees=inventory,
            gitlink_state_repositories=repositories,
        ),
        repo,
        gitlink,
    )


def test_host_extractor_preserves_unchanged_materialized_gitlink(tmp_path: Path) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "materialized-worktree")
    (worktree / "source.txt").write_text("fixed\n", encoding="utf-8")

    assert (worktree / gitlink / "dependency.py").is_file()
    assert "+fixed" in patcher._extract_patch_from_copy(worktree, baseline)


def test_host_extractor_preserves_a_tracked_gitlink_symlink_within_the_task_root(
    tmp_path: Path,
) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(
        tmp_path,
        tracked_parent_link=True,
    )
    worktree = _worktree(repo, tmp_path / "materialized-tracked-link")

    assert (worktree / gitlink / "source-link").resolve() == worktree / "source.txt"
    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_gitignored_materialized_gitlink_residue(tmp_path: Path) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "materialized-ignored-residue")
    cache = worktree / gitlink / "__pycache__"
    cache.mkdir()
    (cache / "dependency.cpython-311.pyc").write_bytes(b"runtime cache")
    (worktree / "source.txt").write_text("fixed\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "+fixed" in extracted
    assert "__pycache__" not in extracted


def test_host_extractor_projects_gitlink_content_hidden_by_solver_ignore(tmp_path: Path) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "materialized-added-ignore")
    nested = worktree / gitlink / "generated"
    nested.mkdir()
    (nested / ".gitignore").write_text("*\n", encoding="utf-8")
    (nested / "answer.py").write_text("ANSWER = True\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "generated/.gitignore" in extracted
    assert "generated/answer.py" in extracted


def test_host_extractor_does_not_publish_ignored_gitlink_symlink_residue(tmp_path: Path) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "materialized-outward-link")
    cache = worktree / gitlink / "__pycache__"
    cache.mkdir()
    (cache / "oracle.pyc").symlink_to("/eval_input/test_patch")

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def test_host_extractor_ignores_materialized_gitlink_index_only_deletion(
    tmp_path: Path,
) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "deleted-materialized-gitlink")
    _git(worktree, "rm", "--cached", "--quiet", gitlink)

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert extracted == ""


@pytest.mark.parametrize("change", ["modify", "add", "delete"])
def test_host_extractor_projects_materialized_gitlink_changes(
    tmp_path: Path,
    change: str,
) -> None:
    baseline, repo, gitlink = _materialized_gitlink_baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / f"materialized-{change}")
    module = worktree / gitlink
    if change == "modify":
        (module / "dependency.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif change == "add":
        (module / "new.py").write_text("NEW = True\n", encoding="utf-8")
    else:
        shutil.rmtree(module)

    extracted = patcher._extract_patch_from_copy(worktree, baseline)
    assert "deleted file mode 160000" in extracted
    if change == "delete":
        assert "new file mode 100644" not in extracted
    else:
        assert "new file mode 100644" in extracted


def test_host_extractor_excludes_opencollab_state(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "state")
    state = worktree / ".opencollab"
    state.mkdir()
    (state / "events.jsonl").write_text("secret\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert ".opencollab" not in extracted


def test_host_extractor_excludes_retirement_tombstones(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "retired")
    nested = worktree / "src"
    nested.mkdir()
    (nested / ".opencollab-retired-deadbeef").write_text("x", encoding="utf-8")

    assert patcher._extract_patch_from_copy(worktree, baseline) == ""


def _proof(snapshot: SolverGitSnapshot, patch: str) -> dict:
    encoded = patch.encode("utf-8")
    return patcher.TrustedPatchExtraction(
        fixed_anonymous_base=snapshot.anonymous_head,
        base_tree=snapshot.base_tree,
        baseline_archive_sha256="a" * 64,
        baseline_archive_bytes=100,
        baseline_archive_entries=2,
        baseline_extracted_bytes=50,
        workspace_archive_sha256="b" * 64,
        workspace_archive_bytes=120,
        workspace_archive_entries=3,
        workspace_extracted_bytes=60,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
        candidate_tree="c" * 40,
        changed_paths=(),
        path_modes=(),
    ).as_dict()


def test_current_proof_binds_snapshot_patch_and_summary() -> None:
    snapshot = SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 1)
    patch = "diff --git a/a b/a\n"
    metric = {
        **current_inline_generation_schema_fields(),
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": snapshot.as_dict(),
        "trusted_patch_extraction": _proof(snapshot, patch),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
    }

    assert current_generation_proof_valid(metric, patch)
    assert current_generation_summary_proof_valid(metric)
    assert not current_generation_proof_valid(metric, patch + "x")
    metric["generation_image_id"] = "mutable:latest"
    assert not current_generation_proof_valid(metric, patch)
    metric["generation_image_id"] = "sha256:" + "8" * 64
    metric["trusted_patch_extraction"]["workspace_archive_entries"] = True
    assert not current_generation_summary_proof_valid(metric)


def test_empty_patch_requires_exact_zero_byte_proof() -> None:
    snapshot = SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 0)
    metric = {
        **current_inline_generation_schema_fields(),
        "generation_image_id": "sha256:" + "8" * 64,
        "solver_git_snapshot": snapshot.as_dict(),
        "trusted_patch_extraction": _proof(snapshot, ""),
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert current_generation_proof_valid(metric, "")
    metric["trusted_patch_extraction"]["patch_bytes"] = 1
    assert not current_generation_proof_valid(metric, "")


def test_extract_requires_quiescence_before_and_after_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline, _repo = _baseline(tmp_path)
    calls = 0

    def quiesce(_container: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("still running")

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        (root / ".git").mkdir()
        return "c" * 64, 10, 1, 0

    monkeypatch.setattr(patcher, "require_container_quiescence", quiesce)
    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    with pytest.raises(RuntimeError, match="still running"):
        patcher.extract_patch_trusted("cid", baseline)
    assert calls == 2


def test_workspace_copy_retries_a_truncated_archive_in_a_fresh_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots: list[Path] = []
    quiescence_calls = 0

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        roots.append(root)
        (root / "partial").write_text("discard me", encoding="utf-8")
        if len(roots) == 1:
            raise patcher._WorkspaceArchiveTruncated("unexpected end of data")
        (root / "complete").write_text("keep me", encoding="utf-8")
        return "c" * 64, 10, 2, 14

    def quiesce(_container: str) -> None:
        nonlocal quiescence_calls
        quiescence_calls += 1

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "require_container_quiescence", quiesce)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    root, archive = patcher._copy_frozen_workspace("cid", tmp_path)

    assert archive == ("c" * 64, 10, 2, 14)
    assert len(roots) == 2
    assert roots[0] != roots[1]
    assert not roots[0].exists()
    assert root == tmp_path / "workspace"
    assert (root / "complete").read_text(encoding="utf-8") == "keep me"
    assert quiescence_calls == 2


def test_workspace_copy_discards_both_truncated_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots: list[Path] = []

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        roots.append(root)
        (root / "partial").write_text("discard me", encoding="utf-8")
        raise patcher._WorkspaceArchiveTruncated("unexpected end of data")

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "require_container_quiescence", lambda _container: None)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    with pytest.raises(patcher._WorkspaceArchiveTruncated, match="unexpected end"):
        patcher._copy_frozen_workspace("cid", tmp_path)

    assert len(roots) == 2
    assert not any(root.exists() for root in roots)
    assert not (tmp_path / "workspace").exists()


def test_workspace_copy_does_not_retry_a_policy_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        nonlocal calls
        calls += 1
        (root / "partial").write_text("discard me", encoding="utf-8")
        raise RuntimeError("container workspace archive exceeded its byte limit")

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())

    with pytest.raises(RuntimeError, match="byte limit"):
        patcher._copy_frozen_workspace("cid", tmp_path)

    assert calls == 1
    assert not (tmp_path / ".workspace-copy-1").exists()


def test_workspace_copy_stops_when_a_truncated_attempt_cannot_be_discarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        nonlocal calls
        calls += 1
        (root / "partial").write_text("discard me", encoding="utf-8")
        raise patcher._WorkspaceArchiveTruncated("unexpected end of data")

    def refuse_cleanup(_root: Path) -> None:
        raise PermissionError("cleanup refused")

    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())
    monkeypatch.setattr(patcher.shutil, "rmtree", refuse_cleanup)

    with pytest.raises(PermissionError, match="cleanup refused"):
        patcher._copy_frozen_workspace("cid", tmp_path)

    assert calls == 1


def test_trusted_baseline_rejects_workspace_digest_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = SolverGitSnapshot(
        "a" * 40, "b" * 40, 1, 0, 0, 1, workspace_sha256="0" * 64
    )

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        (root / ".git").mkdir()
        (root / "source.py").write_text("drifted = True\n", encoding="utf-8")
        return "c" * 64, 10, 2, 15

    monkeypatch.setattr(patcher, "require_container_quiescence", lambda _container: None)
    monkeypatch.setattr(patcher, "frozen_container", lambda _container: nullcontext())
    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)

    with pytest.raises(RuntimeError, match="does not match snapshot evidence"):
        patcher.prepare_trusted_patch_baseline("cid", snapshot)


def test_baseline_cleanup_removes_trusted_temporary_directory(tmp_path: Path) -> None:
    temp = patcher.tempfile.TemporaryDirectory(dir=tmp_path)
    location = Path(temp.name)
    baseline = patcher.TrustedPatchBaseline(
        snapshot=SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 0),
        temporary_directory=temp,
        git_dir=location / "repo.git",
        archive_sha256="c" * 64,
        archive_bytes=1,
        archive_entries=1,
        extracted_bytes=0,
    )
    baseline.cleanup()
    assert not location.exists()
