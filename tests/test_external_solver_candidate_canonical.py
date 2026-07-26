from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import external_solver_usage as esu
from opencollab_eval.generation.candidate_patch import construct_candidate_patch


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True)


@pytest.mark.parametrize("change", ["rename", "copy"])
def test_external_canonical_patch_matches_trusted_candidate_for_file_moves(
    tmp_path: Path,
    change: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "-q", str(repository))
    _git("-C", str(repository), "config", "user.name", "Evaluator")
    _git("-C", str(repository), "config", "user.email", "evaluator@example.invalid")
    source = repository / "source.txt"
    original = "".join(f"stable line {index:03d}\n" for index in range(100))
    source.write_text(original, encoding="utf-8")
    _git("-C", str(repository), "add", "source.txt")
    _git("-C", str(repository), "commit", "-qm", "baseline")
    base = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    trusted = tmp_path / "trusted.git"
    _git("clone", "-q", "--bare", str(repository), str(trusted))
    _git(f"--git-dir={trusted}", "config", "diff.renames", "copies")
    target = repository / f"{change}.txt"
    if change == "rename":
        source.rename(target)
    else:
        target.write_text(original, encoding="utf-8")
        source.write_text(original.replace("stable line 050", "updated line 050"), encoding="utf-8")

    candidate = construct_candidate_patch(
        git_dir=trusted,
        worktree=repository,
        base=base,
        baseline_sha256="a" * 64,
        max_patch_bytes=1024 * 1024,
    )
    tree, canonical = esu._canonical_candidate_from_patch(trusted, base, candidate.patch)

    assert tree == candidate.candidate_tree
    assert canonical == candidate.patch
