"""Candidate dependency preparation across benchmark shell environments."""

from __future__ import annotations

import errno
import subprocess
from types import SimpleNamespace

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
