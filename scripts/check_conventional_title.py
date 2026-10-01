#!/usr/bin/env python3
"""Validate the Conventional Commit title used by a PR or direct main push."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

_TITLE = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|revert)"
    r"(\([a-z0-9._/ -]+\))?!?: .+$"
)
_CHINESE = re.compile(r"[\u3400-\u9fff]")
_ZERO_SHA = "0" * 40
_GITHUB_MERGE = re.compile(r"^Merge pull request #[1-9][0-9]* from \S+$")
# Preserve titles in the reviewed ICLR history when importing it into main.
# Descendants and unrelated commits remain subject to the current convention.
_PRESERVED_HISTORY_TIPS = ("d7cd9e167ce6b52d3d2a65a2de81a237f3fd8100",)


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def validate_title(title: str) -> str | None:
    """Return an error message when *title* violates the repository convention."""
    if "\n" in title or "\r" in title:
        return "title must be a single line"
    if not _TITLE.fullmatch(title):
        return "title must follow Conventional Commits"
    if not _CHINESE.search(title):
        return "title summary must contain Chinese text"
    return None


def commit_subject(repository: Path, commit: str) -> str:
    """Read the canonical subject from the commit object named by *commit*."""
    completed = subprocess.run(
        ["git", "--no-replace-objects", "show", "-s", "--format=%s", commit],
        cwd=repository,
        check=True,
        capture_output=True,
        env=_git_environment(),
        text=True,
    )
    return completed.stdout.rstrip("\n")


def commits_in_range(repository: Path, base: str, head: str) -> list[str]:
    """Return pushed commits in oldest-first order."""
    revision = head if base == _ZERO_SHA else f"{base}..{head}"
    completed = subprocess.run(
        ["git", "--no-replace-objects", "rev-list", "--reverse", revision],
        cwd=repository,
        check=True,
        capture_output=True,
        env=_git_environment(),
        text=True,
    )
    return completed.stdout.split()


def _preserved_history(repository: Path) -> set[str]:
    commits: set[str] = set()
    for tip in _PRESERVED_HISTORY_TIPS:
        available = subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "-e", f"{tip}^{{commit}}"],
            cwd=repository, capture_output=True, env=_git_environment(),
        )
        if available.returncode:
            continue
        completed = subprocess.run(
            ["git", "--no-replace-objects", "rev-list", tip],
            cwd=repository, check=True, capture_output=True,
            env=_git_environment(), text=True,
        )
        commits.update(completed.stdout.split())
    return commits


def _commit_title_error(repository: Path, commit: str, subject: str) -> str | None:
    error = validate_title(subject)
    if error is None or not _GITHUB_MERGE.fullmatch(subject):
        return error
    completed = subprocess.run(
        ["git", "--no-replace-objects", "show", "-s", "--format=%P%n%b", commit],
        cwd=repository,
        check=True,
        capture_output=True,
        env=_git_environment(),
        text=True,
    )
    parents, _, body = completed.stdout.partition("\n")
    if len(parents.split()) < 2:
        return error
    # GitHub stores the PR title in the merge message body. Validate it only
    # for an actual merge; range mode still checks every introduced commit.
    pr_title = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return validate_title(pr_title)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--title")
    source.add_argument("--commit")
    source.add_argument("--range", dest="commit_range", nargs=2, metavar=("BASE", "HEAD"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.commit_range is not None:
            base, head = args.commit_range
            commits = commits_in_range(Path.cwd(), base, head)
            if not commits:
                print("::error::No pushed commits were available for title validation.")
                return 1
            preserved = _preserved_history(Path.cwd())
            current_commits = [commit for commit in commits if commit not in preserved]
            for commit in current_commits:
                title = commit_subject(Path.cwd(), commit)
                error = _commit_title_error(Path.cwd(), commit, title)
                if error:
                    print(
                        f"::error::{error}. Commit {commit} has title {title!r}."
                    )
                    return 1
            print(
                f"Conventional title checks passed for {len(current_commits)} commits. "
                f"Preserved {len(commits) - len(current_commits)} reviewed historical titles."
            )
            return 0
        title = (
            args.title
            if args.title is not None
            else commit_subject(Path.cwd(), args.commit)
        )
        error = (
            validate_title(title)
            if args.title is not None
            else _commit_title_error(Path.cwd(), args.commit, title)
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::Unable to read commit title: {exc}")
        return 2
    if error:
        print(f"::error::{error}. Received {title!r}.")
        return 1
    print(f"Conventional title check passed for {title!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
