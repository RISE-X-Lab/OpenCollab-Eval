"""Behavior tests for PR and pushed-commit title validation."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(
    os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1])
).resolve()
_SCRIPT = _REPO_ROOT / "scripts" / "check_conventional_title.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("check_conventional_title", _SCRIPT)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
validate_title = _SCRIPT_MODULE.validate_title
_CLEAN_SNAPSHOT = "chore: \u5efa\u7acb\u5e72\u51c0\u53d1\u5e03\u5feb\u7167"
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
    assert validate_title("fix(runtime)!: \u4fee\u590d\u4f1a\u8bdd\u7ec8\u6001") is None


def test_title_rejects_invalid_type_english_summary_and_multiple_lines():
    assert validate_title("change: \u66f4\u65b0") == "title must follow Conventional Commits"
    assert validate_title("fix: repair runtime") == "title summary must contain Chinese text"
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
        "fix: \u4fee\u590d\u6700\u7ec8\u63d0\u4ea4",
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
        "test: \u589e\u52a0\u9996\u4e2a\u6d4b\u8bd5",
    )
    (repository / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "commit",
        "-m",
        "fix: \u4fee\u590d\u7b2c\u4e8c\u4e2a\u6d4b\u8bd5",
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
        "fix: \u4fee\u590d\u6700\u7ec8\u63d0\u4ea4",
    )

    result = _run(repository, "--range", _ZERO_SHA, "HEAD")

    assert result.returncode == 1
    assert "invalid root title" in result.stdout


def test_range_mode_fails_when_git_history_is_missing(tmp_path):
    repository = _repository(tmp_path, _CLEAN_SNAPSHOT)

    result = _run(repository, "--range", "missing-base", "HEAD")

    assert result.returncode == 2
    assert "Unable to read commit title" in result.stdout
