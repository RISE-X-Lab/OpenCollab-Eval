"""Behavior tests for PR and pushed-commit title validation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts import check_conventional_title as checker
from scripts.check_conventional_title import validate_title
from tests.support.paths import SOURCE_ROOT

_REPO_ROOT = SOURCE_ROOT
_CLEAN_SNAPSHOT = "chore: create clean release snapshot"
_ZERO_SHA = "0" * 40


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    return environment


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path, subject: str) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Title Test")
    _git(repository, "config", "user.email", "title@example.invalid")
    (repository / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", subject)
    return repository


def _run(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_conventional_title", *args],
        cwd=repository,
        check=False,
        capture_output=True,
        env=_subprocess_environment(),
        text=True,
    )


def test_title_accepts_repository_convention():
    assert validate_title(_CLEAN_SNAPSHOT) is None
    assert validate_title("fix(runtime)!: preserve terminal state") is None


def test_title_rejects_invalid_type_chinese_summary_and_multiple_lines():
    assert validate_title("change: \u66f4\u65b0") == "title must follow Conventional Commits"
    assert validate_title("fix: \u4fee\u590d") == "title summary must use English"
    assert validate_title("fix: \u4fee\u590d\nsecond line") == "title must be a single line"


def test_commit_mode_reads_the_subject_from_git(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--commit", "HEAD")

    assert result.returncode == 0
    assert "check passed" in result.stdout


def test_commit_mode_fails_when_the_commit_is_missing(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--commit", "missing")

    assert result.returncode == 2
    assert "Unable to read commit title" in result.stdout


def test_range_mode_checks_every_pushed_commit(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "invalid intermediate title")
    (repository / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "commit",
        "-m",
        "fix: repair final commit",
    )

    result = _run(repository, "--range", base, "HEAD")

    assert result.returncode == 1
    assert "invalid intermediate title" in result.stdout


def test_range_mode_accepts_all_valid_titles(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "commit",
        "-m",
        "test: add first test",
    )
    (repository / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "commit",
        "-m",
        "fix: repair second test",
    )

    result = _run(repository, "--range", base, "HEAD")

    assert result.returncode == 0
    assert "2 commits" in result.stdout


def test_range_mode_rejects_zero_commits(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    head = _git(repository, "rev-parse", "HEAD")

    result = _run(repository, "--range", head, head)

    assert result.returncode == 1
    assert "No pushed commits" in result.stdout


def test_zero_sha_range_checks_all_reachable_commits(tmp_path):
    repository = _repository(tmp_path, "invalid root title")
    (repository / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "commit",
        "-m",
        "fix: repair final commit",
    )

    result = _run(repository, "--range", _ZERO_SHA, "HEAD")

    assert result.returncode == 1
    assert "invalid root title" in result.stdout


def test_range_mode_fails_when_git_history_is_missing(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--range", "missing-base", "HEAD")

    assert result.returncode == 2
    assert "Unable to read commit title" in result.stdout


def _github_merge(repository: Path, *, branch_title: str, pr_title: str) -> str:
    base = _git(repository, "rev-parse", "HEAD")
    original_branch = _git(repository, "branch", "--show-current")
    _git(repository, "checkout", "-b", "feature")
    _git(repository, "commit", "--allow-empty", "-m", branch_title)
    _git(repository, "checkout", original_branch)
    _git(repository, "merge", "--no-ff", "feature", "-m", "Merge pull request #28 from org/feature", "-m", pr_title)
    return base


def test_github_merge_validates_its_pr_title_and_introduced_commits(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    base = _github_merge(
        repository,
        branch_title="fix: repair scoring execution",
        pr_title="fix(eval): repair scoring execution",
    )
    assert _run(repository, "--commit", "HEAD").returncode == 0
    result = _run(repository, "--range", base, "HEAD")
    assert result.returncode == 0
    assert "2 commits" in result.stdout


def test_github_merge_does_not_hide_invalid_branch_commit(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    base = _github_merge(
        repository,
        branch_title="invalid branch change",
        pr_title="fix: repair scoring execution",
    )
    result = _run(repository, "--range", base, "HEAD")
    assert result.returncode == 1
    assert "invalid branch change" in result.stdout


def test_github_merge_requires_conventional_english_pr_title(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    _github_merge(repository, branch_title="fix: repair scoring execution", pr_title="fix: \u4fee\u590d\u8bc4\u5206")
    result = _run(repository, "--commit", "HEAD")
    assert result.returncode == 1
    assert "English" in result.stdout


def test_single_parent_commit_cannot_impersonate_a_github_merge(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    _git(repository, "commit", "--allow-empty", "-m", "Merge pull request #28 from org/feature",
         "-m", "fix: repair scoring execution")
    assert _run(repository, "--commit", "HEAD").returncode == 1


def test_pr_title_cannot_use_a_merge_subject():
    assert validate_title("Merge pull request #28 from org/feature") is not None


def test_range_preserves_reviewed_titles_and_checks_new_descendants(tmp_path, monkeypatch):
    repository = _repository(tmp_path, "historical imported title")
    reviewed = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(checker, "_PRESERVED_HISTORY_TIPS", (reviewed,))
    monkeypatch.chdir(repository)
    _git(repository, "commit", "--allow-empty", "-m", _CLEAN_SNAPSHOT)

    assert checker.main(["--range", _ZERO_SHA, "HEAD"]) == 0

    _git(repository, "commit", "--allow-empty", "-m", "invalid new descendant")
    assert checker.main(["--range", _ZERO_SHA, "HEAD"]) == 1


def test_reviewed_history_does_not_exempt_an_unrelated_branch(tmp_path, monkeypatch):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-b", "reviewed")
    _git(repository, "commit", "--allow-empty", "-m", "historical imported title")
    reviewed = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-b", "other", base)
    _git(repository, "commit", "--allow-empty", "-m", "invalid unrelated title")
    _git(repository, "merge", "--no-ff", "reviewed", "-m", _CLEAN_SNAPSHOT)
    monkeypatch.setattr(checker, "_PRESERVED_HISTORY_TIPS", (reviewed,))
    monkeypatch.chdir(repository)

    assert checker.main(["--range", base, "HEAD"]) == 1


def test_title_rejects_cjk_extensions_and_unreadable_summaries():
    for summary in ("\U00020000", "12345", "\u200b"):
        assert validate_title(f"fix: {summary}") is not None
    assert validate_title("docs: clarify café setup — preserve Unicode punctuation") is None
