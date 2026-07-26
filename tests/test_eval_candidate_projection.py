from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
    eval_snapshot_proof_fields,
)

import opencollab_eval.engine.eval_candidate_projection as projection_module
from opencollab_eval.engine.eval_candidate_projection import (
    CandidateProjectionError,
    build_prepared_projection,
    build_source_projection,
    candidate_projection_valid,
    verify_prepared_worktree,
)
from opencollab_eval.engine.swe_v1_remote_artifacts import _read_candidate_projection
from opencollab_eval.generation.gen_prediction_snapshot_support import anonymous_commit_oid


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "OpenCollab Eval")
    _git(repository, "config", "user.email", "eval@example.invalid")
    (repository / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repository, "add", "calculator.py")
    _git(repository, "commit", "-qm", "baseline")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    (repository / "new_module.py").write_text("ANSWER = 42\n")
    _git(repository, "add", "new_module.py")
    patch = _git(repository, "diff", "--binary", "--full-index", "HEAD") + "\n"
    patch += _git(repository, "diff", "--binary", "--full-index", "--cached", "HEAD") + "\n"
    _git(repository, "reset", "--hard", "-q", "HEAD")
    return repository, base_commit, patch


def _expectation(
    patch: str,
    *,
    expected_tree: str = "",
    base_commit: str = "",
    base_tree: str = "",
) -> dict[str, str]:
    digest = hashlib.sha256(patch.encode()).hexdigest()
    return {
        "schema": "opencollab.eval_candidate_expectation.v1",
        "instance_id": "task-1",
        "record_id": "a" * 32,
        "run_identity_sha256": "b" * 64,
        "source_patch_sha256": digest,
        "eval_patch_sha256": digest,
        "source_base_commit": base_commit,
        "source_anonymous_base": anonymous_commit_oid(base_tree) if base_tree else "",
        "source_base_tree": base_tree,
        "source_candidate_tree": expected_tree,
        "expected_candidate_tree": expected_tree,
    }


def _write_inputs(tmp_path: Path, patch: str, expectation: dict[str, str]) -> tuple[Path, Path]:
    patch_path = tmp_path / "candidate.patch"
    expectation_path = tmp_path / "expectation.json"
    patch_path.write_text(patch)
    expectation_path.write_text(json.dumps(expectation))
    return patch_path, expectation_path


def test_projection_binds_patch_to_fresh_base_tree(tmp_path: Path) -> None:
    repository, base_commit, patch = _repository(tmp_path)
    base_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    patch_path, expectation_path = _write_inputs(
        tmp_path,
        patch,
        _expectation(patch, base_commit=base_commit, base_tree=base_tree),
    )
    first = build_source_projection(repository, base_commit, patch_path, expectation_path)
    expected = _expectation(
        patch,
        expected_tree=first["verified_source_candidate_tree"],
        base_commit=base_commit,
        base_tree=base_tree,
    )
    expectation_path.write_text(json.dumps(expected))

    source = build_source_projection(repository, base_commit, patch_path, expectation_path)
    source_path = tmp_path / "source-projection.json"
    source_path.write_text(json.dumps(source))
    projection = build_prepared_projection(
        repository, base_commit, patch_path, expectation_path, source_path
    )
    projection_path = tmp_path / "projection.json"
    projection_path.write_text(json.dumps(projection))
    subprocess.run(["git", "-C", str(repository), "apply", str(patch_path)], check=True)
    projection = verify_prepared_worktree(repository, patch_path, projection_path)

    assert projection["status"] == "verified"
    assert projection["verified_source_base_tree"] == base_tree
    assert projection["verified_source_candidate_tree"] == expected["expected_candidate_tree"]
    assert projection["prepared_candidate_tree"] == projection["worktree_candidate_tree"]
    assert projection["generation_tree_matches"] is True
    assert projection["official_worktree_matches"] is True


def test_projection_rejects_patch_and_tree_identity_drift(tmp_path: Path) -> None:
    repository, base_commit, patch = _repository(tmp_path)
    patch_path, expectation_path = _write_inputs(
        tmp_path,
        patch,
        _expectation(patch, expected_tree="f" * 40),
    )
    with pytest.raises(CandidateProjectionError, match="differs from generation"):
        build_source_projection(repository, base_commit, patch_path, expectation_path)

    expectation_path.write_text(json.dumps(_expectation(patch)))
    patch_path.write_text(patch + "\n")
    with pytest.raises(CandidateProjectionError, match="SHA-256"):
        build_source_projection(repository, base_commit, patch_path, expectation_path)


def test_legacy_projection_keeps_full_v1_identity_checks() -> None:
    expectation, current = candidate_eval_proof_fields("task-1", "record-1", "a" * 64)
    legacy = {
        "schema": "opencollab.eval_candidate_projection.v1",
        "status": "verified",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "base_commit": expectation["source_anonymous_base"],
        "base_tree": expectation["source_base_tree"],
        "candidate_tree": current["verified_source_candidate_tree"],
        "generation_tree_matches": True,
    }

    assert candidate_projection_valid(legacy, expectation) is True
    legacy["source_anonymous_base"] = "9" * 40
    assert candidate_projection_valid(legacy, expectation) is False


@pytest.mark.parametrize(
    "field",
    ["source_base_commit", "source_anonymous_base", "source_base_tree"],
)
def test_projection_rejects_generation_baseline_drift(tmp_path: Path, field: str) -> None:
    repository, base_commit, patch = _repository(tmp_path)
    base_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    expectation = _expectation(patch, base_commit=base_commit, base_tree=base_tree)
    expectation[field] = "f" * len(expectation[field])
    patch_path, expectation_path = _write_inputs(tmp_path, patch, expectation)

    with pytest.raises(CandidateProjectionError, match="base.* differs from generation"):
        build_source_projection(repository, base_commit, patch_path, expectation_path)


def test_projection_clears_inherited_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base_commit, patch = _repository(tmp_path)
    patch_path, expectation_path = _write_inputs(tmp_path, patch, _expectation(patch))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    observed: list[set[str]] = []
    real_run = subprocess.run

    def recording_run(*args, **kwargs):
        observed.append({key for key in kwargs["env"] if key.startswith("GIT_")})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(projection_module.subprocess, "run", recording_run)

    assert build_source_projection(
        repository, base_commit, patch_path, expectation_path
    )["status"] == "verified"
    assert observed and all(
        keys == {
            "GIT_ATTR_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_NO_REPLACE_OBJECTS",
        }
        or keys
        == {
            "GIT_ATTR_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_INDEX_FILE",
            "GIT_NO_REPLACE_OBJECTS",
        }
        for keys in observed
    )


def test_filtered_eval_patch_keeps_source_identity_without_claiming_tree_equality(
    tmp_path: Path,
) -> None:
    repository, base_commit, patch = _repository(tmp_path)
    base_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    expectation = _expectation(patch, base_commit=base_commit, base_tree=base_tree)
    expectation.update(source_patch_sha256="f" * 64, source_candidate_tree="e" * 40)
    patch_path, expectation_path = _write_inputs(tmp_path, patch, expectation)

    source = build_source_projection(repository, base_commit, patch_path, expectation_path)
    source_path = tmp_path / "filtered-source.json"
    source_path.write_text(json.dumps(source))
    prepared = build_prepared_projection(
        repository, base_commit, patch_path, expectation_path, source_path
    )

    assert source["generation_tree_matches"] is None
    assert prepared["verified_source_candidate_tree"] != expectation["source_candidate_tree"]


def test_projection_reader_separates_source_and_public_prepared_trees(tmp_path: Path) -> None:
    expectation, projection = candidate_eval_proof_fields("task-1", "record-1", "a" * 64)
    snapshot = eval_snapshot_proof_fields()
    snapshot.update(anonymous_head="9" * 40, base_tree="8" * 40)
    projection.update(
        prepared_base_commit="9" * 40,
        prepared_base_tree="8" * 40,
        prepared_candidate_tree="7" * 40,
        worktree_candidate_tree="7" * 40,
    )
    (tmp_path / "candidate_projection.json").write_text(json.dumps(projection))
    (tmp_path / "source_candidate_projection.json").write_text(
        json.dumps(candidate_source_projection_fields(expectation))
    )
    errors: list[str] = []

    result, source = _read_candidate_projection(tmp_path, errors, expectation, snapshot)

    assert errors == []
    assert source["verified_source_base_tree"] == "b" * 40
    assert result["verified_source_base_tree"] == "b" * 40
    assert result["prepared_base_tree"] == "8" * 40


def _gitlink_repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    repository = tmp_path / "gitlink-repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "OpenCollab Eval")
    _git(repository, "config", "user.email", "eval@example.invalid")
    (repository / "value.txt").write_text("baseline\n")
    _git(repository, "add", "value.txt")
    old_oid = "1" * 40
    _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{old_oid},integration")
    _git(repository, "commit", "-qm", "baseline with gitlink")
    return repository, _git(repository, "rev-parse", "HEAD"), old_oid, "2" * 40


def _source_with_expected_tree(
    tmp_path: Path, repository: Path, base: str, patch: str
) -> tuple[Path, Path, dict[str, str]]:
    base_tree = _git(repository, "rev-parse", f"{base}^{{tree}}")
    patch_path, expectation_path = _write_inputs(
        tmp_path, patch, _expectation(patch, base_commit=base, base_tree=base_tree)
    )
    source = build_source_projection(repository, base, patch_path, expectation_path)
    expectation = _expectation(
        patch,
        expected_tree=source["verified_source_candidate_tree"],
        base_commit=base,
        base_tree=base_tree,
    )
    expectation_path.write_text(json.dumps(expectation))
    source = build_source_projection(repository, base, patch_path, expectation_path)
    source_path = tmp_path / "gitlink-source.json"
    source_path.write_text(json.dumps(source))
    return patch_path, expectation_path, source


def test_projection_preserves_a_gitlink_when_public_setup_changes_its_oid(
    tmp_path: Path,
) -> None:
    repository, source_base, _old_oid, prepared_oid = _gitlink_repository(tmp_path)
    (repository / "value.txt").write_text("candidate\n")
    patch = _git(
        repository, "diff", "--binary", "--full-index", "HEAD", "--", "value.txt"
    ) + "\n"
    _git(repository, "checkout", "--", "value.txt")
    patch_path, expectation_path, source = _source_with_expected_tree(
        tmp_path, repository, source_base, patch
    )
    source_path = tmp_path / "gitlink-source.json"
    _git(repository, "update-index", "--cacheinfo", f"160000,{prepared_oid},integration")
    _git(repository, "commit", "-qm", "public setup changes gitlink")
    prepared_base = _git(repository, "rev-parse", "HEAD")
    prepared = build_prepared_projection(
        repository, prepared_base, patch_path, expectation_path, source_path
    )
    projection_path = tmp_path / "gitlink-prepared.json"
    projection_path.write_text(json.dumps(prepared))
    subprocess.run(["git", "-C", str(repository), "apply", str(patch_path)], check=True)

    verified = verify_prepared_worktree(repository, patch_path, projection_path)

    assert verified["verified_source_candidate_tree"] == source["verified_source_candidate_tree"]
    assert verified["official_worktree_matches"] is True


@pytest.mark.parametrize("change", ["delete", "oid"])
def test_projection_checks_gitlink_candidate_changes(tmp_path: Path, change: str) -> None:
    repository, base, _old_oid, new_oid = _gitlink_repository(tmp_path)
    if change == "delete":
        _git(repository, "rm", "--cached", "integration")
    else:
        _git(repository, "update-index", "--cacheinfo", f"160000,{new_oid},integration")
    patch = _git(repository, "diff", "--cached", "--binary", "--full-index", "HEAD") + "\n"
    _git(repository, "reset", "--hard", "-q", "HEAD")
    patch_path, expectation_path, _source = _source_with_expected_tree(
        tmp_path, repository, base, patch
    )
    source_path = tmp_path / "gitlink-source.json"
    prepared = build_prepared_projection(
        repository, base, patch_path, expectation_path, source_path
    )
    projection_path = tmp_path / "gitlink-prepared.json"
    projection_path.write_text(json.dumps(prepared))
    subprocess.run(
        ["git", "-C", str(repository), "apply", str(patch_path)],
        check=True,
        capture_output=True,
    )

    if change == "delete":
        assert verify_prepared_worktree(repository, patch_path, projection_path)[
            "official_worktree_matches"
        ] is True
    else:
        with pytest.raises(CandidateProjectionError, match="candidate worktree content differs"):
            verify_prepared_worktree(repository, patch_path, projection_path)


def test_projection_rejects_gitlink_symlink_to_external_repository(tmp_path: Path) -> None:
    (tmp_path / "external").mkdir()
    external, external_head, _patch = _repository(tmp_path / "external")
    repository, base, _old_oid, _new_oid = _gitlink_repository(tmp_path)
    _git(repository, "update-index", "--cacheinfo", f"160000,{external_head},integration")
    patch = _git(repository, "diff", "--cached", "--binary", "--full-index", "HEAD") + "\n"
    _git(repository, "reset", "--hard", "-q", "HEAD")
    patch_path, expectation_path, _source = _source_with_expected_tree(
        tmp_path, repository, base, patch
    )
    source_path = tmp_path / "gitlink-source.json"
    prepared = build_prepared_projection(
        repository, base, patch_path, expectation_path, source_path
    )
    projection_path = tmp_path / "gitlink-symlink.json"
    projection_path.write_text(json.dumps(prepared))
    subprocess.run(["git", "-C", str(repository), "apply", str(patch_path)], check=True)
    (repository / "integration").rmdir()
    os.symlink(external, repository / "integration")

    with pytest.raises(CandidateProjectionError, match="worktree type differs"):
        verify_prepared_worktree(repository, patch_path, projection_path)


def test_worktree_projection_ignores_filters_and_preserves_file_types(tmp_path: Path) -> None:
    repository, base, _initial = _repository(tmp_path)
    _git(repository, "config", "filter.upper.clean", "tr a-z A-Z")
    (repository / ".gitattributes").write_text("*.txt filter=upper\n")
    (repository / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    (repository / "raw.txt").write_text("lowercase\n")
    (repository / "binary.bin").write_bytes(b"\x00\xffraw\n")
    (repository / "tool.sh").write_text("#!/bin/sh\nexit 0\n")
    (repository / "tool.sh").chmod(0o755)
    os.symlink("calculator.py", repository / "calculator-link")
    info_attributes = repository / ".git" / "info" / "attributes"
    info_attributes.write_text("* -filter\n")
    _git(repository, "add", "-A")
    patch = _git(repository, "diff", "--cached", "--binary", "--full-index", "HEAD") + "\n"
    _git(repository, "reset", "--hard", "-q", "HEAD")
    _git(repository, "clean", "-fdq")
    info_attributes.unlink()
    patch_path, expectation_path, _source = _source_with_expected_tree(
        tmp_path, repository, base, patch
    )
    source_path = tmp_path / "gitlink-source.json"
    prepared = build_prepared_projection(
        repository, base, patch_path, expectation_path, source_path
    )
    projection_path = tmp_path / "filter-projection.json"
    projection_path.write_text(json.dumps(prepared))
    subprocess.run(["git", "-C", str(repository), "apply", str(patch_path)], check=True)

    verified = verify_prepared_worktree(repository, patch_path, projection_path)

    assert verified["official_worktree_matches"] is True
    assert (repository / "raw.txt").read_text() == "lowercase\n"


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_anonymous_commit_identity_matches_git(tmp_path: Path, object_format: str) -> None:
    repository = tmp_path / object_format
    init = subprocess.run(
        ["git", "init", "-q", f"--object-format={object_format}", str(repository)],
        text=True,
        capture_output=True,
        check=False,
    )
    if init.returncode != 0:
        pytest.skip(f"Git does not support {object_format}")
    tree = _git(repository, "write-tree")
    environment = os.environ.copy()
    environment.update(
        GIT_AUTHOR_NAME="OpenCollab Solver Snapshot",
        GIT_AUTHOR_EMAIL="solver-snapshot@invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
        GIT_COMMITTER_NAME="OpenCollab Solver Snapshot",
        GIT_COMMITTER_EMAIL="solver-snapshot@invalid",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00",
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repository), "commit-tree", tree],
        input="solver snapshot\n",
        text=True,
        env=environment,
    ).strip()

    assert anonymous_commit_oid(tree) == commit
