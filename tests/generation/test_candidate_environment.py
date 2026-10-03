"""Candidate dependency preparation across benchmark shell environments."""

from __future__ import annotations

import errno
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import candidate_environment_lean as candidate_environment
from opencollab_eval.generation import candidate_runtime, gen_prediction_config, gen_prediction_snapshot


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_candidate_runtime_hydrates_ignored_dependency_root(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "tracked.txt")
    _git(
        source,
        "-c",
        "user.name=OpenCollab Test",
        "-c",
        "user.email=test@opencollab.invalid",
        "commit",
        "-m",
        "baseline",
    )
    dependency = source / "node_modules" / "example"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
    executable = source / "node_modules" / ".bin" / "review-tool"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf 'dependency-ready\\n'\n", encoding="utf-8")
    executable.chmod(0o755)

    store = tmp_path / "candidate-runtime"
    candidate_runtime.prepare(source, ["node_modules"], store, candidate_count=1)
    candidate = tmp_path / "candidate"
    _git(source, "worktree", "add", "--detach", str(candidate), "HEAD")

    assert not (candidate / "node_modules").exists()
    assert candidate_runtime.hydrate(store, candidate) is True
    assert (candidate / "node_modules" / "example" / "index.js").read_text(
        encoding="utf-8"
    ) == "module.exports = 42;\n"
    executed = subprocess.run(
        [str(candidate / "node_modules" / ".bin" / "review-tool")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert executed.stdout == "dependency-ready\n"
    assert candidate_runtime.hydrate(store, candidate) is False


def test_candidate_runtime_copies_ignored_root_across_mounts(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "tracked.txt")
    _git(source, "-c", "user.name=OpenCollab Test", "-c",
         "user.email=test@opencollab.invalid", "commit", "-m", "baseline")
    dependency = source / "node_modules" / "example"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
    store = tmp_path / "candidate-runtime"
    candidate_runtime.prepare(source, ["node_modules"], store, candidate_count=1)
    candidate = tmp_path / "candidate"
    _git(source, "worktree", "add", "--detach", str(candidate), "HEAD")

    original_rename = candidate_runtime.Path.rename

    def cross_mount_rename(path, target):
        if path.name == "node_modules" and path.parent.parent.name == "claims":
            raise OSError(errno.EXDEV, "cross-device link")
        return original_rename(path, target)

    monkeypatch.setattr(candidate_runtime.Path, "rename", cross_mount_rename)
    assert candidate_runtime.hydrate(store, candidate) is True
    installed = candidate / "node_modules"
    assert installed.is_dir() and not installed.is_symlink()
    assert (installed / "example" / "index.js").read_text(encoding="utf-8") == "module.exports = 42;\n"
    status = subprocess.run(["git", "-C", str(candidate), "status", "--porcelain"],
                            check=True, capture_output=True, text=True)
    assert status.stdout == ""


def _candidate_dependencies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / ".gitignore").write_text("node_modules/\nvendor/\n")
    _git(source, "add", ".gitignore")
    _git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-m", "fixture")
    for root in ("node_modules", "vendor"):
        (source / root).mkdir()
        (source / root / "dependency.txt").write_text(root)
    store = tmp_path / "runtime"
    candidate_runtime.prepare(source, ["node_modules", "vendor"], store, candidate_count=1)
    candidate = tmp_path / "candidate"
    _git(source, "worktree", "add", "--detach", str(candidate), "HEAD")
    return store, candidate


@pytest.mark.parametrize("failed_root", ["node_modules", "vendor"])
def test_candidate_runtime_returns_fully_rolled_back_claim_for_retry(tmp_path, monkeypatch, failed_root):
    store, candidate = _candidate_dependencies(tmp_path)
    original_rename = Path.rename
    fault_pending = True

    def transient_failure(path, target):
        nonlocal fault_pending
        if fault_pending and path.name == failed_root and path.parent.parent.name == "claims":
            fault_pending = False
            raise OSError(errno.EIO, "transient dependency I/O failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", transient_failure)
    with pytest.raises(OSError, match="transient dependency I/O failure"):
        candidate_runtime.hydrate(store, candidate)
    assert not list((store / "claims").iterdir())
    assert len(list((store / "ready").iterdir())) == 1
    assert not any((candidate / root).exists() for root in ("node_modules", "vendor"))
    assert candidate_runtime.hydrate(store, candidate) is True
    for root in ("node_modules", "vendor"):
        assert (candidate / root / "dependency.txt").read_text() == root


def test_candidate_runtime_keeps_one_writer_for_simultaneous_hydration(tmp_path):
    store, candidate = _candidate_dependencies(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(candidate_runtime.hydrate, store, candidate) for _ in range(2)]
        assert sorted(future.result(timeout=5) for future in futures) == [False, True]
    assert len(list((store / "claims").iterdir())) == 1
    assert (store / "state" / "candidate.json").exists()
    for root in ("node_modules", "vendor"):
        assert (candidate / root / "dependency.txt").read_text() == root


def test_candidate_runtime_marker_write_failure_returns_claim_after_rollback(tmp_path, monkeypatch):
    store, candidate = _candidate_dependencies(tmp_path)
    marker = store / "state" / "candidate.json"
    original_write = Path.write_text
    fault_pending = True

    def partial_marker_failure(path, *args, **kwargs):
        nonlocal fault_pending
        if path == marker and fault_pending:
            fault_pending = False
            original_write(path, "partial marker")
            raise OSError(errno.EIO, "transient marker I/O failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", partial_marker_failure)
    with pytest.raises(OSError, match="transient marker I/O failure"):
        candidate_runtime.hydrate(store, candidate)
    assert not marker.exists()
    assert not list((store / "claims").iterdir())
    assert candidate_runtime.hydrate(store, candidate) is True


def test_candidate_environment_reuses_prepare_python_for_hydration(monkeypatch) -> None:
    captured: dict[str, object] = {}
    python = "/opt/control-plane/bin/python3"

    monkeypatch.setattr(
        candidate_environment,
        "image_activation_prefix",
        lambda *_args, **_kwargs: "activate",
    )
    monkeypatch.setattr(candidate_environment, "image_helper_python", lambda _cid: python)
    monkeypatch.setattr(gen_prediction_config, "_workspace_archive_timeout_from_env", lambda: 45)
    monkeypatch.setattr(
        gen_prediction_snapshot,
        "_install_snapshot_helper",
        lambda container_id, source, target: captured.update(
            {"installed": (container_id, source.name, target)}
        ),
    )

    def prepare(*args, **kwargs):
        captured["prepare_args"] = args
        captured["prepare_kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(gen_prediction_snapshot, "_docker_with_stdin", prepare)
    runtime = SimpleNamespace(
        store="/.opencollab-generation-runtime-abc",
        workspace="/testbed",
        roots=("node_modules",),
    )

    prefix = candidate_environment.install_candidate_environment(
        "container-id",
        runtime,
        activation="activate-testbed",
    )

    assert captured["prepare_args"][5] == python
    assert captured["prepare_args"][9] == "/tmp/.opencollab-generation-runtime-abc-candidates"
    assert captured["prepare_kwargs"] == {
        "input_text": '["node_modules"]',
        "timeout": 45,
    }
    wrapped = prefix("node -e 'require(\"example\")'")
    assert f"{python} /tmp/opencollab_candidate_runtime.py hydrate" in wrapped
    assert "/tmp/.opencollab-generation-runtime-abc-candidates" in wrapped
    assert "activate\nnode -e" in wrapped
