from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.engine.workspace_integrity import WorkspaceIntegrityError
from opencollab_eval.generation import gen_prediction_snapshot as snapshot
from opencollab_eval.generation import gen_prediction_snapshot_container as snapshot_container
from opencollab_eval.generation import gen_prediction_snapshot_support as snapshot_support


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
    (repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    executable = repo / "tool.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return repo, git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")


def test_snapshot_git_calls_bind_the_discovered_repository_as_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "foreign-owner-repo"
    repo.mkdir()
    observed = []

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(snapshot_container.subprocess, "run", fake_run)

    snapshot_container._run_git(repo, "status", env={})

    assert observed[0][0][:5] == [
        "git",
        "-c",
        f"safe.directory={repo.resolve()}",
        "-C",
        str(repo),
    ]


def test_snapshot_rebuilds_one_disposable_base_and_reports_sanitation(tmp_path: Path) -> None:
    repo, base, base_tree = repository(tmp_path)
    (repo / "answer.txt").write_text("future answer", encoding="utf-8")
    (repo / "build.cache").write_text("cache", encoding="utf-8")
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "other.txt").write_text("other task", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert evidence["expected_base_commit"] == base
    assert evidence["base_tree"] == base_tree
    assert evidence["commit_count"] == 1
    assert git(repo, "rev-list", "--all", "--count") == "1"
    assert git(repo, "remote") == ""
    assert not (repo / "answer.txt").exists()
    assert not (repo / "build.cache").exists()
    assert not nested.exists()
    assert os.access(repo / "tool.sh", os.X_OK)
    report = evidence["workspace_integrity"]
    assert report["outcome"] == "sanitize_then_continue"
    assert {item["action"] for item in report["findings"]} >= {"sanitize_then_continue"}
    assert all(item["failure_scope"] == "none" for item in report["findings"])


def test_snapshot_removes_future_refs_objects_and_remotes(tmp_path: Path) -> None:
    repo, base, base_tree = repository(tmp_path)
    (repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "commit", "-qam", "future")
    git(repo, "branch", "future-answer")
    git(repo, "checkout", "-q", base)
    git(repo, "remote", "add", "origin", "https://example.invalid/private.git")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert evidence["base_tree"] == base_tree
    assert git(repo, "rev-list", "--all", "--count") == "1"
    assert git(repo, "show-ref") != base
    assert git(repo, "remote") == ""
    fsck = subprocess.run(
        ["git", "-C", str(repo), "fsck", "--full", "--unreachable", "--no-reflogs"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not fsck.stdout.strip()


def test_snapshot_sanitizes_tracked_image_drift_before_solver(tmp_path: Path) -> None:
    repo, base, base_tree = repository(tmp_path)
    (repo / "source.py").write_text("VALUE = 999\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert evidence["base_tree"] == base_tree
    assert (repo / "source.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    finding = next(
        item
        for item in evidence["workspace_integrity"]["findings"]
        if item["observed_state"]["kind"] == "tracked_content_drift"
    )
    assert finding["action"] == "sanitize_then_continue"
    assert finding["verified_state_after_action"] == "restored_from_verified_base"


def test_snapshot_blocks_unknown_tracked_drift_when_state_must_be_preserved(
    tmp_path: Path,
) -> None:
    repo, base, _tree = repository(tmp_path)
    (repo / "source.py").write_text("VALUE = 999\n", encoding="utf-8")

    with pytest.raises(snapshot_container.SnapshotSetupError) as raised:
        snapshot_container.create_solver_snapshot(repo, base, preserve_workspace_state=True)

    assert raised.value.failure_scope.value == "image"
    assert raised.value.integrity_report["outcome"] == "task_technical_failure"
    assert (repo / "source.py").read_text(encoding="utf-8") == "VALUE = 999\n"


def test_snapshot_records_tracked_public_preparation_in_the_solver_baseline(tmp_path: Path) -> None:
    repo, base, _tree = repository(tmp_path)
    (repo / "source.py").write_text("PUBLIC TEST PREPARATION\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert (repo / "source.py").read_text(encoding="utf-8") == "PUBLIC TEST PREPARATION\n"
    finding = next(
        item
        for item in evidence["workspace_integrity"]["findings"]
        if item["observed_state"]["kind"] == "tracked_content_drift"
    )
    assert finding["action"] == "allow"
    assert finding["verified_state_after_action"] == "recorded_as_public_preparation_baseline"


def test_public_preparation_can_read_an_explicit_future_object_before_it_is_removed(
    tmp_path: Path,
) -> None:
    repo, base, _tree = repository(tmp_path)
    (repo / "public_test.py").write_text("EXPECTED = 2\n", encoding="utf-8")
    git(repo, "add", "public_test.py")
    git(repo, "commit", "-qm", "future public test")
    future = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", base)
    (repo / "answer.txt").write_text("leak", encoding="utf-8")

    preparation = snapshot_container.prepare_public_input(repo, base)
    git(repo, "checkout", future, "--", "public_test.py")
    evidence = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert preparation["worktree_matches_base"] is True
    assert not (repo / "answer.txt").exists()
    assert (repo / "public_test.py").read_text(encoding="utf-8") == "EXPECTED = 2\n"
    assert evidence["expected_base_commit"] == base
    assert git(repo, "rev-list", "--all", "--count") == "1"
    assert subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", future],
        capture_output=True,
        check=False,
    ).returncode != 0


def test_public_preparation_removes_private_git_behavior(tmp_path: Path) -> None:
    repo, _base, _tree = repository(tmp_path)
    (repo / ".gitattributes").write_text("public_test.py filter=private\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "attributes")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "public_test.py").write_text("EXPECTED = 2\n", encoding="utf-8")
    git(repo, "add", "public_test.py")
    git(repo, "commit", "-qm", "future public test")
    future = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", base)
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "config", "filter.private.smudge", "sed s/EXPECTED/PRIVATE/")
    git(repo, "config", "filter.private.required", "true")

    snapshot_container.prepare_public_input(repo, base)
    git(repo, "checkout", future, "--", "public_test.py")

    assert (repo / "public_test.py").read_text(encoding="utf-8") == "EXPECTED = 2\n"
    assert not marker.exists()
    config_probe = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-regexp", r"^filter\."],
        capture_output=True,
        text=True,
        check=False,
    )
    assert config_probe.returncode == 1
    assert config_probe.stdout == ""


def test_git_config_symlink_is_replaced_without_touching_its_target(tmp_path: Path) -> None:
    repo, _base, _tree = repository(tmp_path)
    config = repo / ".git" / "config"
    external = tmp_path / "external-config"
    external.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(external)

    snapshot_support.sanitize_preparation_repository(repo, "sha1")

    assert not config.is_symlink()
    assert external.read_bytes() != config.read_bytes()
    assert b"repositoryformatversion" in config.read_bytes()


def test_workspace_digest_binds_file_and_empty_directory_modes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    file = root / "data.txt"
    file.write_text("same bytes", encoding="utf-8")
    empty = root / "empty"
    empty.mkdir()
    original = snapshot_support.workspace_sha256(root)
    file.chmod(0o444)
    file_mode_changed = snapshot_support.workspace_sha256(root)
    empty.chmod(0o700)
    directory_mode_changed = snapshot_support.workspace_sha256(root)

    assert file_mode_changed != original
    assert directory_mode_changed != file_mode_changed


def test_public_preparation_status_evidence_is_bounded(tmp_path: Path) -> None:
    repo, base, _tree = repository(tmp_path)
    for index in range(3000):
        (repo / f"residue-{index:04d}.txt").write_text("x", encoding="utf-8")

    evidence = snapshot_container.prepare_public_input(repo, base)

    finding = next(
        item
        for item in evidence["workspace_integrity"]["findings"]
        if item["observed_state"]["kind"] == "untracked_content"
    )
    detail = json.loads(finding["observed_state"]["detail"])
    assert detail["count"] == 3000
    assert len(detail["sample"]) <= 32
    assert detail["truncated"] is True
    assert len(json.dumps(evidence).encode()) < snapshot._MAX_EVIDENCE_BYTES


def test_public_preparation_checks_a_late_outward_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "answer.txt"
    outside.write_text("answer", encoding="utf-8")
    repo, base, _tree = repository(tmp_path)
    snapshot_container.prepare_public_input(repo, base)
    for index in range(2050):
        (repo / f"public-{index:04d}.txt").write_text("x", encoding="utf-8")
    (repo / "zz-answer-link").symlink_to(outside)

    with pytest.raises(snapshot_container.SnapshotSetupError):
        snapshot_container.create_solver_snapshot(
            repo,
            base,
            baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
            preserve_workspace_state=True,
        )


def test_public_preparation_preserves_and_binds_an_empty_directory(tmp_path: Path) -> None:
    repo, base, _tree = repository(tmp_path)
    before = snapshot_container.prepare_public_input(repo, base)
    empty = repo / "public-empty"
    empty.mkdir(mode=0o700)

    after = snapshot_container.create_solver_snapshot(
        repo,
        base,
        baseline_drift_origin=snapshot_container.FindingOrigin.PUBLIC_INPUT,
        preserve_workspace_state=True,
    )

    assert empty.is_dir()
    assert after["workspace_sha256"] != before["workspace_sha256"]


@pytest.mark.parametrize("leak_kind", ("tracked_drift", "untracked", "future_ref"))
def test_initial_eval_snapshot_hides_image_state_before_public_preparation(
    tmp_path: Path,
    leak_kind: str,
) -> None:
    repo, base, _tree = repository(tmp_path)
    marker = tmp_path / "leaked"
    if leak_kind == "tracked_drift":
        (repo / "setup.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('leaked')\n",
            encoding="utf-8",
        )
        git(repo, "add", "setup.py")
        git(repo, "commit", "-qm", "public setup")
        base = git(repo, "rev-parse", "HEAD")
        (repo / "setup.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('drift')\n",
            encoding="utf-8",
        )
    elif leak_kind == "untracked":
        (repo / "answer.txt").write_text("reference answer", encoding="utf-8")
    else:
        (repo / "answer.txt").write_text("reference answer", encoding="utf-8")
        git(repo, "add", "answer.txt")
        git(repo, "commit", "-qm", "future answer")
        git(repo, "branch", "future-answer")
        git(repo, "checkout", "-q", base)

    snapshot_container.create_solver_snapshot(repo, base)
    if leak_kind == "tracked_drift":
        assert "write_text('leaked')" in (repo / "setup.py").read_text(encoding="utf-8")
        assert "write_text('drift')" not in (repo / "setup.py").read_text(encoding="utf-8")
        assert not marker.exists()
    elif leak_kind == "untracked":
        assert not (repo / "answer.txt").exists()
    else:
        probe = subprocess.run(
            ["git", "show", "future-answer:answer.txt"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0


def test_snapshot_allows_a_trusted_broken_outward_symlink(tmp_path: Path) -> None:
    repo, _base, _tree = repository(tmp_path)
    os.symlink("../../missing-runtime-target", repo / "optional-link")
    git(repo, "add", "optional-link")
    git(repo, "commit", "-qm", "link")
    base = git(repo, "rev-parse", "HEAD")

    evidence = snapshot_container.create_solver_snapshot(repo, base)

    assert (repo / "optional-link").is_symlink()
    assert os.readlink(repo / "optional-link") == "../../missing-runtime-target"
    link_finding = next(
        item
        for item in evidence["workspace_integrity"]["findings"]
        if item["observed_state"]["kind"] == "outward_symlink"
    )
    assert link_finding["action"] == "allow"


def test_snapshot_blocks_a_readable_outward_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "answer.txt"
    outside.write_text("reference answer", encoding="utf-8")
    repo, _base, _tree = repository(tmp_path)
    os.symlink("../answer.txt", repo / "answer-link")
    git(repo, "add", "answer-link")
    git(repo, "commit", "-qm", "link")
    base = git(repo, "rev-parse", "HEAD")

    with pytest.raises(snapshot_container.SnapshotSetupError) as raised:
        snapshot_container.create_solver_snapshot(repo, base)

    assert raised.value.failure_scope.value == "image"


def test_snapshot_failure_leaves_original_repository_unchanged(tmp_path: Path) -> None:
    outside = tmp_path / "answer.txt"
    outside.write_text("reference answer", encoding="utf-8")
    repo, _base, _tree = repository(tmp_path)
    (repo / "answer-link").symlink_to(outside)
    git(repo, "add", "answer-link")
    git(repo, "commit", "-qm", "add outward link")
    base = git(repo, "rev-parse", "HEAD")
    original_git = git(repo, "rev-parse", "HEAD")

    with pytest.raises(snapshot_container.SnapshotSetupError):
        snapshot_container.create_solver_snapshot(repo, base)

    assert git(repo, "rev-parse", "HEAD") == original_git
    assert (repo / ".git").is_dir()
    assert (repo / "answer-link").is_symlink()


def test_workspace_replacement_supports_a_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "mounted"
    prepared = tmp_path / "prepared"
    original.mkdir()
    prepared.mkdir()
    (original / "old.txt").write_text("old", encoding="utf-8")
    (prepared / "new.txt").write_text("new", encoding="utf-8")
    rename = Path.rename

    def cross_device_rename(path: Path, target: Path) -> Path:
        if path == original:
            raise OSError(errno.EXDEV, "cross-device link")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", cross_device_rename)

    snapshot_container._replace_workspace(original, prepared)

    assert not (original / "old.txt").exists()
    assert (original / "new.txt").read_text(encoding="utf-8") == "new"
    assert not prepared.exists()


def valid_evidence() -> dict[str, object]:
    return {
        "enabled": True,
        "anonymous_head": "a" * 40,
        "base_tree": "b" * 40,
        "workspace_sha256": "0" * 64,
        "commit_count": 1,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": 1,
        "removed_gitlinks": [],
        "materialized_gitlinks": [],
        "expected_base_commit": "c" * 40,
        "workspace_integrity": {
            "schema": "opencollab.workspace_integrity.v1",
            "findings": [],
            "outcome": "allow",
            "failure_scope": "none",
        },
    }


def test_host_wrapper_installs_policy_and_snapshot_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    stdin_calls: list[tuple[tuple[str, ...], str]] = []
    evidence = valid_evidence()

    def fake_docker(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_stdin(*args, input_text):
        stdin_calls.append((args, input_text))
        return subprocess.CompletedProcess(args, 0, json.dumps(evidence), "")

    monkeypatch.setattr(snapshot, "_docker", fake_docker)
    monkeypatch.setattr(snapshot, "_docker_with_stdin", fake_stdin)

    result = snapshot.prepare_solver_git_snapshot("container", "c" * 40)

    assert [call[0] for call in calls] == ["cp", "cp", "cp"]
    assert str(snapshot._CONTAINER_POLICY_HELPER_SOURCE) in calls[0]
    assert stdin_calls[0][0][:5] == ("exec", "-i", "-w", "/tmp", "container")
    assert stdin_calls[0][1] == "c" * 40 + "\n"
    assert result.as_dict() == evidence


def test_host_wrapper_preserves_structured_image_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [
            {
                "observed_state": {"kind": "outward_symlink"},
                "classification_basis": "solver-visible state has no safe provenance",
                "action": "task_technical_failure",
                "verified_state_after_action": "task_workspace_not_started",
                "failure_scope": "image",
            }
        ],
        "outcome": "task_technical_failure",
        "failure_scope": "image",
    }
    monkeypatch.setattr(
        snapshot,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        snapshot,
        "_docker_with_stdin",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            json.dumps({"enabled": False, "workspace_integrity": report}),
            "solver workspace snapshot failed [image]",
        ),
    )

    with pytest.raises(WorkspaceIntegrityError) as raised:
        snapshot.prepare_solver_git_snapshot("container", "c" * 40)

    assert raised.value.failure_scope.value == "image"
    assert raised.value.integrity_report == report


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(commit_count=2),
        lambda value: value.update(expected_base_commit="short"),
        lambda value: value["workspace_integrity"].update(failure_scope="image"),
        lambda value: value["workspace_integrity"].update(outcome="task_technical_failure"),
    ],
)
def test_host_parser_rejects_incomplete_integrity_evidence(mutation) -> None:
    evidence = valid_evidence()
    mutation(evidence)
    with pytest.raises(RuntimeError):
        snapshot._parse_snapshot_output(json.dumps(evidence))


def test_anonymous_solver_ids_are_unique_and_opaque() -> None:
    first = snapshot.anonymous_solver_task_id()
    second = snapshot.anonymous_solver_task_id()
    assert first != second
    assert first.startswith("solver-")
    assert len(first) == len("solver-") + 32
