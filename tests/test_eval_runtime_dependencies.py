"""Tests for trusted image runtime dependency preservation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.commands import swe_final_report_model as final_report_model
from opencollab_eval.engine.eval_runtime_dependencies import _within_root, restore, stash
from opencollab_eval.engine.swe_test_plan_contract import (
    legacy_javascript_runtime_dependencies,
    previous_javascript_runtime_dependencies,
    validated_test_plan_kind,
)
from opencollab_eval.engine.swe_v1_remote_target_proof import plan_runtime_dependency_specs
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan


def _repository(tmp_path: Path, *, ignored: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if ignored:
        (repo / ".gitignore").write_text("vendor-runtime/\n", encoding="utf-8")
    runner = repo / "vendor-runtime" / ".bin" / "runner"
    implementation = repo / "vendor-runtime" / "runner-package" / "run.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("print('ok')\n", encoding="utf-8")
    implementation.chmod(0o755)
    runner.parent.mkdir(parents=True)
    runner.symlink_to("../runner-package/run.py")
    return repo


def _spec(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "root": "vendor-runtime",
                    "required_paths": ["vendor-runtime/.bin/runner"],
                    "kind": "directory",
                    "candidate_protected": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_plan_runtime_dependency_specs_use_structured_requirements():
    plan = {
        "runtime_dependencies": [
            {
                "root": "node_modules",
                "required_paths": ["node_modules/.bin/jest"],
                "kind": "directory",
                "candidate_protected": True,
            }
        ]
    }

    assert plan_runtime_dependency_specs(plan) == [
        {
            "root": "node_modules",
            "required_paths": ["node_modules/.bin/jest"],
            "kind": "directory",
            "candidate_protected": True,
        }
    ]


def test_original_v2_javascript_runtime_dependency_is_normalized_and_validated():
    plan = prolite_test_plan(
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "js",
            "selected_test_files_to_run": ["test/a.js"],
            "test_patch": "diff --git a/test/a.js b/test/a.js\n",
        },
        ["test/a.js | works"],
        target_file="/eval_input/f2p.targets.json",
    )
    plan["runtime_dependencies"] = [
        {
            "root": "node_modules",
            "required_paths": ["node_modules/.bin/mocha"],
        }
    ]

    assert validated_test_plan_kind(plan, require_commands=True) == "mocha-json-stream"
    assert plan_runtime_dependency_specs(plan) == [
        {
            "root": "node_modules",
            "required_paths": ["node_modules/.bin/mocha"],
            "kind": "directory",
            "candidate_protected": True,
        }
    ]


def test_previous_file_aware_javascript_runtime_dependency_remains_valid():
    plan = prolite_test_plan(
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "js",
            "selected_test_files_to_run": ["test/a.js"],
            "test_patch": "diff --git a/test/a.js b/test/a.js\n",
        },
        ["test/a.js | works"],
        target_file="/eval_input/f2p.targets.json",
    )
    plan["runtime_dependencies"] = plan["runtime_dependencies"][:2]

    assert validated_test_plan_kind(plan, require_commands=True) == "mocha-json-stream"


@pytest.mark.parametrize(
    "historical_dependencies",
    [previous_javascript_runtime_dependencies, legacy_javascript_runtime_dependencies],
)
def test_final_report_normalizes_only_declared_historical_javascript_dependencies(
    historical_dependencies,
):
    trusted = prolite_test_plan(
        {"repo_language": "javascript", "repo": "nodebb/nodebb"},
        ["test/topics.js | title"],
        target_file="/eval_input/f2p.targets.json",
    )
    historical = json.loads(json.dumps(trusted))
    historical["runtime_dependencies"] = historical_dependencies(trusted["adapter"])

    final_report_model._normalize_historical_runtime_dependencies(historical, trusted)

    assert historical == trusted


def test_final_report_does_not_normalize_unknown_javascript_dependencies():
    trusted = prolite_test_plan(
        {"repo_language": "javascript", "repo": "nodebb/nodebb"},
        ["test/topics.js | title"],
        target_file="/eval_input/f2p.targets.json",
    )
    forged = json.loads(json.dumps(trusted))
    forged["runtime_dependencies"] = [{"root": "answer.json", "required_paths": ["answer.json"]}]

    final_report_model._normalize_historical_runtime_dependencies(forged, trusted)

    assert forged != trusted


def test_stash_and_restore_preserve_only_the_discovered_runner_root(tmp_path: Path):
    repo = _repository(tmp_path)
    store = tmp_path / "store"
    output = tmp_path / "runtime.json"

    manifest = stash(repo, _spec(tmp_path / "spec.json"), store)

    assert manifest["phase"] == "stashed"
    assert not (repo / "vendor-runtime").exists()
    report = restore(repo, store, output)
    assert report["phase"] == "restored"
    assert report["solver_visible"] is False
    assert os.access(repo / "vendor-runtime" / ".bin" / "runner", os.X_OK)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_stash_and_restore_preserve_an_ignored_runtime_manifest(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("vendor-runtime/\n/package.json\n", encoding="utf-8")
    manifest_path = repo / "package.json"
    manifest_path.write_text('{"scripts":{"test":"mocha"}}\n', encoding="utf-8")
    spec_path = tmp_path / "manifest-spec.json"
    spec_path.write_text(
        json.dumps(
            [
                {
                    "root": "package.json",
                    "required_paths": ["package.json"],
                    "kind": "file",
                    "candidate_protected": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    store = tmp_path / "store"
    expected_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    manifest = stash(repo, spec_path, store)

    assert manifest["entries"][0]["content_sha256"] == expected_sha
    assert not manifest_path.exists()
    report = restore(repo, store, tmp_path / "runtime.json")
    assert manifest_path.read_text(encoding="utf-8") == '{"scripts":{"test":"mocha"}}\n'
    assert report["entries"] == [
        {
            "root": "package.json",
            "required_paths": ["package.json"],
            "kind": "file",
            "candidate_protected": False,
            "content_sha256": expected_sha,
        }
    ]


def test_stash_leaves_a_tracked_runtime_manifest_in_the_baseline(tmp_path: Path):
    repo = _repository(tmp_path)
    manifest_path = repo / "package.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "package.json"], check=True)
    spec_path = tmp_path / "manifest-spec.json"
    spec_path.write_text(
        json.dumps(
            [
                {
                    "root": "package.json",
                    "required_paths": ["package.json"],
                    "kind": "file",
                    "candidate_protected": False,
                }
            ]
        ),
        encoding="utf-8",
    )

    manifest = stash(repo, spec_path, tmp_path / "store")

    assert manifest["entries"] == []
    assert manifest_path.read_text(encoding="utf-8") == "{}\n"


def test_stash_rejects_an_ignored_directory_masquerading_as_a_runtime_file(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("vendor-runtime/\n/package.json/\n", encoding="utf-8")
    (repo / "package.json").mkdir()
    spec_path = tmp_path / "manifest-spec.json"
    spec_path.write_text(
        json.dumps(
            [
                {
                    "root": "package.json",
                    "required_paths": ["package.json"],
                    "kind": "file",
                    "candidate_protected": False,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a regular file"):
        stash(repo, spec_path, tmp_path / "store")


def test_restore_rejects_a_runtime_file_with_changed_content_identity(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("vendor-runtime/\n/package.json\n", encoding="utf-8")
    (repo / "package.json").write_text("{}\n", encoding="utf-8")
    spec_path = tmp_path / "manifest-spec.json"
    spec_path.write_text(
        json.dumps(
            [
                {
                    "root": "package.json",
                    "required_paths": ["package.json"],
                    "kind": "file",
                    "candidate_protected": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    store = tmp_path / "store"
    manifest = stash(repo, spec_path, store)
    (store / manifest["entries"][0]["stored"]).write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="content identity changed"):
        restore(repo, store, tmp_path / "runtime.json")


def test_runtime_root_check_supports_python_without_path_is_relative_to(tmp_path: Path):
    root = tmp_path / "root"

    assert _within_root(root / "child", root) is True
    assert _within_root(tmp_path / "outside", root) is False


def test_stash_and_restore_fall_back_across_filesystems(tmp_path: Path, monkeypatch):
    repo = _repository(tmp_path)
    original = os.replace

    def cross_device(_source, _target):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "replace", cross_device)
    store = tmp_path / "store"
    stash(repo, _spec(tmp_path / "spec.json"), store)
    restore(repo, store, tmp_path / "runtime.json")
    monkeypatch.setattr(os, "replace", original)

    assert os.access(repo / "vendor-runtime" / ".bin" / "runner", os.X_OK)


@pytest.mark.parametrize(
    ("repo", "language", "selected", "target", "adapter", "required_path"),
    [
        (
            "protonmail/webclients",
            "js",
            "x.test.ts",
            "x.test.ts | works",
            "jest-json-verbose",
            "node_modules/.bin/jest",
        ),
        (
            "NodeBB/NodeBB",
            "js",
            "test/a.js",
            "test/a.js | works",
            "mocha-json-stream",
            "node_modules/.bin/mocha",
        ),
        (
            "tutao/tutanota",
            "ts",
            "test/tests/FooTest.ts",
            "test/tests/FooTest.ts | Foo",
            "ospec-structured-results",
            "node_modules",
        ),
    ],
)
def test_javascript_plans_carry_structured_runtime_requirements(
    repo, language, selected, target, adapter, required_path
):
    plan = prolite_test_plan(
        {
            "repo": repo,
            "repo_language": language,
            "selected_test_files_to_run": [selected],
            "test_patch": f"diff --git a/{selected} b/{selected}\n",
        },
        [target],
        target_file="/eval_input/f2p.targets.json",
    )

    assert plan["adapter"] == adapter
    assert plan["runtime_dependencies"] == [
        {
            "root": "node_modules",
            "required_paths": [required_path],
            "kind": "directory",
            "candidate_protected": True,
        },
        {
            "root": "package.json",
            "required_paths": ["package.json"],
            "kind": "file",
            "candidate_protected": False,
        },
        {
            "root": "config.json",
            "required_paths": ["config.json"],
            "kind": "file",
            "candidate_protected": False,
        },
    ]


def test_stash_rejects_an_unignored_dependency_root(tmp_path: Path):
    repo = _repository(tmp_path, ignored=False)

    with pytest.raises(ValueError, match="lacks trusted image provenance"):
        stash(repo, _spec(tmp_path / "spec.json"), tmp_path / "store")


def test_stash_rejects_a_runner_that_escapes_its_dependency_root(tmp_path: Path):
    repo = _repository(tmp_path)
    external = repo / "external-runner"
    external.write_text("#!/bin/sh\n", encoding="utf-8")
    external.chmod(0o755)
    runner = repo / "vendor-runtime" / ".bin" / "runner"
    runner.unlink()
    runner.symlink_to("../../external-runner")

    with pytest.raises(ValueError, match="escapes its dependency root"):
        stash(repo, _spec(tmp_path / "spec.json"), tmp_path / "store")


@pytest.mark.parametrize(
    "spec",
    [
        [
            {
                "root": "../escape",
                "required_paths": ["../escape/.bin/run"],
                "kind": "directory",
                "candidate_protected": True,
            }
        ],
        [
            {
                "root": "/absolute",
                "required_paths": ["/absolute/.bin/run"],
                "kind": "directory",
                "candidate_protected": True,
            }
        ],
        [
            {
                "root": "vendor-runtime",
                "required_paths": ["other/.bin/run"],
                "kind": "directory",
                "candidate_protected": True,
            }
        ],
    ],
)
def test_stash_rejects_unsafe_or_unbound_specs(tmp_path: Path, spec):
    repo = _repository(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError):
        stash(repo, spec_path, tmp_path / "store")
