from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation.candidate_patch import (
    CandidateConstructionError,
    GitlinkProjection,
    construct_candidate_patch,
)


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Candidate Test")
    _git(repository, "config", "user.email", "candidate@example.invalid")
    (repository / ".gitignore").write_text(".hypothesis/\n*.cache\n", encoding="utf-8")
    (repository / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "delete.txt").write_text("delete me\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    trusted = tmp_path / "trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(repository), str(trusted))
    return repository, trusted, base


def _candidate(worktree: Path, trusted: Path, base: str, **kwargs):
    return construct_candidate_patch(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        baseline_sha256="a" * 64,
        max_patch_bytes=8 * 1024 * 1024,
        **kwargs,
    )


def test_candidate_collects_tracked_changes_deletion_and_untracked_file(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    (worktree / "delete.txt").unlink()
    (worktree / "added.py").write_text("ADDED = True\n", encoding="utf-8")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("added.py", "delete.txt", "source.py")
    assert result.untracked_paths == ("added.py",)
    assert "-VALUE = 1" in result.patch and "+VALUE = 2" in result.patch
    assert "deleted file mode 100644" in result.patch
    assert result.as_dict()["solver_git_metadata_used"] is False


def test_candidate_collects_nested_tracked_deletion(tmp_path: Path) -> None:
    worktree, _trusted, _base = _repository(tmp_path)
    nested = worktree / "package" / "module.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "--quiet", "-m", "nested")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "nested-delete.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    nested.unlink()
    nested.parent.rmdir()

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("package/module.py",)
    assert "deleted file mode 100644" in result.patch


def test_candidate_ignores_unreadable_ignored_cache_without_opening_it(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    cache = worktree / ".hypothesis"
    cache.mkdir()
    (cache / "examples").write_bytes(b"opaque cache")
    (worktree / "repair.py").write_text("FIXED = True\n", encoding="utf-8")
    cache.chmod(0)
    try:
        result = _candidate(worktree, trusted, base)
    finally:
        cache.chmod(0o755)

    assert result.changed_paths == ("repair.py",)
    assert ".hypothesis" not in result.patch


def test_candidate_never_opens_blocking_file_in_ignored_directory(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    worktree, trusted, base = _repository(tmp_path)
    cache = worktree / ".hypothesis"
    cache.mkdir()
    os.mkfifo(cache / "blocking.fifo")
    os.mkfifo(worktree / "ignored.cache")
    (worktree / "repair.py").write_text("FIXED = True\n", encoding="utf-8")

    result = construct_candidate_patch(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        baseline_sha256="a" * 64,
        max_patch_bytes=1024 * 1024,
        timeout=2,
    )

    assert result.changed_paths == ("repair.py",)


def test_candidate_ignores_unwritable_pytest_cache_control_file(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    cache = worktree / ".pytest_cache"
    cache.mkdir()
    (cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8")
    (worktree / "repair.py").write_text("FIXED = True\n", encoding="utf-8")
    cache.chmod(0o555)
    try:
        result = _candidate(worktree, trusted, base)
    finally:
        cache.chmod(0o755)

    assert result.changed_paths == ("repair.py",)
    assert ".pytest_cache" not in result.patch


def test_candidate_does_not_descend_into_unreadable_pytest_cache(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    cache = worktree / ".pytest_cache"
    cache.mkdir()
    (cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (worktree / "repair.py").write_text("FIXED = True\n", encoding="utf-8")
    cache.chmod(0)
    try:
        result = _candidate(worktree, trusted, base)
    finally:
        cache.chmod(0o755)

    assert result.changed_paths == ("repair.py",)


def test_candidate_rejects_unreadable_untracked_candidate(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    candidate = worktree / "candidate.py"
    candidate.write_text("FIXED = True\n", encoding="utf-8")
    candidate.chmod(0)
    try:
        with pytest.raises(CandidateConstructionError, match="unreadable"):
            _candidate(worktree, trusted, base)
    finally:
        candidate.chmod(0o644)


def test_candidate_ignores_solver_git_metadata_and_private_config(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    expected = worktree / "source.py"
    expected.write_text("VALUE = 3\n", encoding="utf-8")
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/forged\n", encoding="utf-8")
    (worktree / ".git" / "config").write_text(
        "[diff]\n\texternal = /bin/false\n[core]\n\texcludesFile = /dev/null\n",
        encoding="utf-8",
    )

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("source.py",)
    assert "+VALUE = 3" in result.patch


def test_candidate_is_unchanged_when_solver_git_directory_is_replaced(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "source.py").write_text("VALUE = 4\n", encoding="utf-8")
    shutil.rmtree(worktree / ".git")
    (worktree / ".git").write_text("forged solver metadata\n", encoding="utf-8")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("source.py",)
    assert "+VALUE = 4" in result.patch


def test_candidate_ignores_solver_git_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "source.py").write_text("VALUE = 5\n", encoding="utf-8")
    shutil.rmtree(worktree / ".git")
    os.mkfifo(worktree / ".git")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("source.py",)
    assert "+VALUE = 5" in result.patch


def test_candidate_preserves_binary_symlink_and_executable_modes(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "binary.dat").write_bytes(b"\x00\x01\xff")
    (worktree / "local-link").symlink_to("source.py")
    executable = worktree / "tool.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    result = _candidate(worktree, trusted, base)

    modes = {path: new for path, _old, new in result.path_modes}
    assert modes == {"binary.dat": "100644", "local-link": "120000", "tool.sh": "100755"}
    assert "GIT binary patch" in result.patch


def test_candidate_rejects_outward_symlink(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "answer").symlink_to("../../reference-answer")

    with pytest.raises(CandidateConstructionError, match="escapes the worktree"):
        _candidate(worktree, trusted, base)


def test_candidate_rejects_symlinked_parent(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    tracked = worktree / "linked" / "answer.py"
    tracked.parent.mkdir()
    tracked.write_text("ANSWER = False\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "--quiet", "-m", "nested base")
    base = _git(worktree, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(tmp_path / "nested-trusted.git"))
    trusted = tmp_path / "nested-trusted.git"
    tracked.unlink()
    tracked.parent.rmdir()
    (worktree / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CandidateConstructionError, match="escapes the worktree"):
        _candidate(worktree, trusted, base)


def test_candidate_projects_hardlink_as_regular_file(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    source = worktree / "hardlink-source.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    linked = worktree / "hardlink.py"
    os.link(source, linked)

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("hardlink-source.py", "hardlink.py")
    assert result.flattened_hardlinks == ("hardlink-source.py",)
    assert source.stat().st_ino != linked.stat().st_ino


def test_candidate_flattens_visible_nested_repository(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    nested = worktree / "nested"
    nested.mkdir()
    _git(nested, "init", "--quiet")
    _git(nested, "config", "user.name", "Nested")
    _git(nested, "config", "user.email", "nested@example.invalid")
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(nested, "add", ".")
    _git(nested, "commit", "--quiet", "-m", "nested")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("nested/module.py",)
    assert result.flattened_repositories == (("nested", "directory"),)
    assert not (nested / ".git").exists()


def test_candidate_rejects_special_untracked_file(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    worktree, trusted, base = _repository(tmp_path)
    fifo = worktree / "candidate.fifo"
    os.mkfifo(fifo)

    with pytest.raises(CandidateConstructionError, match="not representable"):
        _candidate(worktree, trusted, base)


def test_candidate_uses_baseline_gitignore_when_solver_changes_it(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / ".gitignore").write_text(".hypothesis/\nprivate.py\n", encoding="utf-8")
    (worktree / "private.py").write_text("SECRET = True\n", encoding="utf-8")
    (worktree / "public.py").write_text("PUBLIC = True\n", encoding="utf-8")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == (".gitignore", "private.py", "public.py")
    assert "diff --git a/private.py" in result.patch


def test_untracked_gitignore_cannot_hide_itself_or_a_candidate(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    nested = worktree / "new-package"
    nested.mkdir()
    (nested / ".gitignore").write_text("*\n", encoding="utf-8")
    (nested / "answer.py").write_text("ANSWER = True\n", encoding="utf-8")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == ("new-package/.gitignore", "new-package/answer.py")


def test_solver_attributes_cannot_rewrite_candidate_content(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / ".gitattributes").write_text("source.py ident\n", encoding="utf-8")
    (worktree / "source.py").write_text("TOKEN = '$Id: solver-forged $'\n", encoding="utf-8")

    result = _candidate(worktree, trusted, base)

    assert result.changed_paths == (".gitattributes", "source.py")
    assert "+TOKEN = '$Id: solver-forged $'" in result.patch


def _gitlink_repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    worktree, _trusted, _base = _repository(tmp_path)
    oid = "1" * 40
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", oid, "vendor/module")
    _git(worktree, "commit", "--quiet", "-m", "gitlink")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "gitlink-trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    return worktree, trusted, base, oid


def test_candidate_gitlink_identity_comes_from_trusted_projection(tmp_path: Path) -> None:
    worktree, trusted, base, oid = _gitlink_repository(tmp_path)
    _git(worktree, "update-index", "--add", "--cacheinfo", "160000", "2" * 40, "vendor/module")

    result = _candidate(
        worktree,
        trusted,
        base,
        gitlinks=(GitlinkProjection("vendor/module", oid, "preserve"),),
    )

    assert result.patch == ""
    assert result.changed_paths == ()


def test_candidate_requires_explicit_gitlink_projection(tmp_path: Path) -> None:
    worktree, trusted, base, _oid = _gitlink_repository(tmp_path)

    with pytest.raises(CandidateConstructionError, match="every trusted baseline Gitlink"):
        _candidate(worktree, trusted, base)


def test_candidate_rejects_materialized_gitlink_drift(tmp_path: Path) -> None:
    worktree, trusted, base, oid = _gitlink_repository(tmp_path)
    materialized = worktree / "vendor" / "module"
    materialized.mkdir(parents=True)
    (materialized / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(CandidateConstructionError, match="content changed"):
        _candidate(
            worktree,
            trusted,
            base,
            gitlinks=(GitlinkProjection("vendor/module", oid, "preserve", "a" * 64, "b" * 64),),
        )


def test_candidate_binds_and_validates_baseline_identity(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    result = _candidate(worktree, trusted, base)

    assert result.anonymous_base == base
    assert result.baseline_sha256 == "a" * 64
    assert len(result.base_tree) == 40
    with pytest.raises(CandidateConstructionError, match="identity is invalid"):
        construct_candidate_patch(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            baseline_sha256="invalid",
            max_patch_bytes=1024,
        )


def test_candidate_census_is_bounded_before_patch_generation(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(CandidateConstructionError, match="byte limit"):
        construct_candidate_patch(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            baseline_sha256="a" * 64,
            max_patch_bytes=1024,
            max_census_bytes=8,
        )


def test_candidate_file_size_is_bounded_before_git_reads_it(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    (worktree / "candidate.bin").write_bytes(b"x" * 32)

    with pytest.raises(CandidateConstructionError, match="file byte limit"):
        construct_candidate_patch(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            baseline_sha256="a" * 64,
            max_patch_bytes=1024,
            max_file_bytes=16,
        )


def test_changed_tracked_file_size_is_bounded_before_indexing(tmp_path: Path) -> None:
    worktree, _trusted, _base = _repository(tmp_path)
    (worktree / "large.bin").write_bytes(b"x" * 8)
    _git(worktree, "add", "large.bin")
    _git(worktree, "commit", "--quiet", "-m", "large baseline")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "changed-large.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    (worktree / "large.bin").write_bytes(b"y" * 32)

    with pytest.raises(CandidateConstructionError, match="file byte limit"):
        construct_candidate_patch(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            baseline_sha256="a" * 64,
            max_patch_bytes=1024,
            max_file_bytes=16,
        )


def test_empty_directories_count_toward_filesystem_census(tmp_path: Path) -> None:
    worktree, trusted, base = _repository(tmp_path)
    for index in range(6):
        (worktree / f"empty-{index}").mkdir()

    with pytest.raises(CandidateConstructionError, match="filesystem census"):
        construct_candidate_patch(
            git_dir=trusted,
            worktree=worktree,
            base=base,
            baseline_sha256="a" * 64,
            max_patch_bytes=1024,
            max_census_entries=5,
        )


def test_unchanged_large_tracked_file_does_not_block_candidate(tmp_path: Path) -> None:
    worktree, _trusted, _base = _repository(tmp_path)
    (worktree / "large.bin").write_bytes(b"x" * 32)
    _git(worktree, "add", ".")
    _git(worktree, "commit", "--quiet", "-m", "large baseline")
    base = _git(worktree, "rev-parse", "HEAD")
    trusted = tmp_path / "large-trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(worktree), str(trusted))
    (worktree / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = construct_candidate_patch(
        git_dir=trusted,
        worktree=worktree,
        base=base,
        baseline_sha256="a" * 64,
        max_patch_bytes=1024,
        max_file_bytes=16,
    )

    assert result.changed_paths == ("source.py",)


@pytest.mark.parametrize("action", ("delete", "replacement"))
def test_candidate_gitlink_projection_is_explicit(tmp_path: Path, action: str) -> None:
    worktree, trusted, base, oid = _gitlink_repository(tmp_path)
    if action == "replacement":
        replacement = worktree / "vendor" / "module"
        replacement.parent.mkdir(exist_ok=True)
        replacement.write_text("ordinary file\n", encoding="utf-8")

    result = _candidate(
        worktree,
        trusted,
        base,
        gitlinks=(GitlinkProjection("vendor/module", oid, action),),
    )

    assert "deleted file mode 160000" in result.patch
    if action == "replacement":
        assert "new file mode 100644" in result.patch


@pytest.mark.parametrize("replacement", ["delete", "file", "directory"])
def test_gitlink_candidate_replays_over_materialized_baseline(
    tmp_path: Path, replacement: str
) -> None:
    worktree, trusted, base, oid = _gitlink_repository(tmp_path)
    target = worktree / "vendor" / "module"
    if replacement == "file":
        target.parent.mkdir(exist_ok=True)
        target.write_text("ordinary file\n", encoding="utf-8")
    elif replacement == "directory":
        target.mkdir(parents=True)
        (target / "replacement.py").write_text("VALUE = 2\n", encoding="utf-8")
    candidate = _candidate(
        worktree,
        trusted,
        base,
        gitlinks=(
            GitlinkProjection(
                "vendor/module", oid, "delete" if replacement == "delete" else "replacement"
            ),
        ),
    )
    replay = tmp_path / f"replay-{replacement}"
    _git(tmp_path, "clone", "--quiet", str(trusted), str(replay))
    replay_target = replay / "vendor" / "module"
    if replay_target.exists():
        shutil.rmtree(replay_target)
    replay_target.mkdir(parents=True)
    (replay_target / "stale.py").write_text("STALE = True\n", encoding="utf-8")
    shutil.rmtree(replay_target)
    replay_target.mkdir()
    subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        cwd=replay,
        input=candidate.patch.encode(),
        check=True,
    )

    if replacement == "delete":
        assert not replay_target.exists()
    elif replacement == "file":
        assert replay_target.read_text(encoding="utf-8") == "ordinary file\n"
    else:
        assert (replay_target / "replacement.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert not (replay_target / "stale.py").exists()


def test_candidate_module_remains_compact() -> None:
    from opencollab_eval.generation import candidate_patch

    lines = Path(candidate_patch.__file__).read_text(encoding="utf-8").count("\n")
    assert lines < 500
