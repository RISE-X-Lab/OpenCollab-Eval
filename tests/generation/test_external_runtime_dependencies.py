"""Use image dependencies during external execution and exclude them at capture."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import gen_prediction_openhands as external
from opencollab_eval.generation.gen_prediction_snapshot import _parse_snapshot_output
from opencollab_eval.generation.gen_prediction_snapshot_container import create_solver_snapshot
from opencollab_eval.generation.generation_runtime_dependencies import (
    remove_runtime_paths,
    restore_image_dependencies,
    stash_image_dependencies,
)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P2-03: external snapshot removes image dependencies")
def test_external_solver_reuses_ignored_runtime_dependencies_before_trusted_capture(monkeypatch, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the image dependency fixture")
    repo = tmp_path / "testbed"
    repo.mkdir()
    for name, value in {
        ".gitignore": "node_modules/\n",
        "package.json": '{"name":"fixture","dependencies":{"image-dep":"1.0.0"}}\n',
        "package-lock.json": '{"name":"fixture","lockfileVersion":3}\n',
        "probe.js": "console.log(require('image-dep'));\n",
    }.items():
        (repo / name).write_text(value)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    base = git("rev-parse", "HEAD")
    dependency = repo / "node_modules" / "image-dep"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("module.exports = 'image dependency available';\n")
    assert subprocess.run([node, "probe.js"], cwd=repo, capture_output=True).returncode == 0
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"instance_id": "fixture__javascript-1", "base_commit": base,
                                    "repo": "fixture/javascript", "problem_statement": "Repair source."}))
    observed = {}

    def stash(cid, expected):
        store = tmp_path / "runtime"
        roots = stash_image_dependencies(repo, expected, store)
        return SimpleNamespace(store=store, roots=roots)

    def solver(**kwargs):
        observed["solver_exit"] = subprocess.run([node, "probe.js"], cwd=repo, capture_output=True).returncode
        return dict(status="done", returncode=0, execution_quiesced=True,
                    host_execution_quiesced=True, container_execution_quiesced=True)

    def capture(*args):
        observed["runtime_at_capture"] = (repo / "node_modules").exists()
        raise RuntimeError("fixture capture completed")

    monkeypatch.setattr(external.gp, "start_container_with_marker", lambda *a, **k: "fixture-cid")
    monkeypatch.setattr(external.container_guard, "container_image_id", lambda *a: "sha256:" + "8" * 64)
    monkeypatch.setattr(external.gp, "stash_solver_runtime_dependencies", stash)
    monkeypatch.setattr(external.gp, "restore_solver_runtime_dependencies",
                        lambda cid, state: restore_image_dependencies(repo, state.store))
    monkeypatch.setattr(external.gp, "remove_solver_runtime_dependencies",
                        lambda cid, state: remove_runtime_paths(repo, state.roots))
    monkeypatch.setattr(
        external, "prepare_solver_git_snapshot",
        lambda cid, expected: _parse_snapshot_output(json.dumps(create_solver_snapshot(repo, expected))),
    )
    monkeypatch.setattr(external, "prepare_trusted_patch_baseline", lambda *a: SimpleNamespace(cleanup=lambda: None))
    monkeypatch.setattr(external, "_run_openhands", solver)
    monkeypatch.setattr(external, "extract_patch_guarded", capture)
    monkeypatch.setattr(external.gp, "finalize_container_ownership", lambda **k: None)
    monkeypatch.setattr(sys, "argv", ["external", "--instance-file", str(instance), "--output",
                                     str(tmp_path / "output" / "predictions.jsonl"), "--command", "fixture"])

    with pytest.raises(RuntimeError, match="patch extraction or validation-artifact cleanup failed"):
        external.main()
    assert observed["solver_exit"] == 0
    assert observed["runtime_at_capture"] is False
