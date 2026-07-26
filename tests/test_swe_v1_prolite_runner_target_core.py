from __future__ import annotations

# ruff: noqa: F401, F403, F405, I001

import hashlib
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_v1_prolite_runner_test_support import *


def test_patch_fallback_rejects_reversed_patch(tmp_path):
    if shutil.which("patch") is None:
        return
    function = _patch_fallback_function()
    (tmp_path / "file.txt").write_text("new\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode != 0
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert "reversed" in log_file.read_text(encoding="utf-8", errors="replace").lower()


def test_patch_fallback_accepts_verified_already_applied_test_patch(tmp_path):
    if shutil.which("patch") is None:
        return
    function = _patch_fallback_function()
    (tmp_path / "file.txt").write_text("new\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file} "
        "ignore-space-change verify_already_applied\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode == 0
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert "verified test patch already applied" in log_file.read_text(
        encoding="utf-8", errors="replace"
    )


def test_eval_integrity_detects_missing_tests_and_proves_go_targets():
    namespace = _proof_namespace()

    assert namespace["eval_log_has_infra_failure"](4, "collected 0 items") is True
    assert namespace["eval_log_has_infra_failure"](5, "no tests ran") is True
    assert namespace["eval_log_has_infra_failure"](
        1, "no required module provides package example.invalid/dependency"
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1, "request failed: getaddrinfo EAI_AGAIN nodejs.org"
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        4,
        "ERROR: not found: tests/test_feature.py::test_feature\n"
        "collected 0 items / 1 error\nno tests ran\n"
        "ImportError: cannot import name 'feature'",
    ) is False
    go_log = "\n".join(
        [
            json.dumps({"Action": "run", "Test": "TestA"}),
            json.dumps({"Action": "pass", "Test": "TestA"}),
            json.dumps({"Action": "pass", "Test": "TestB/sub"}),
        ]
    )
    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "go"},
        ["TestA", "TestB/sub"],
        0,
        go_log,
    )
    assert proof["ok"] is True
    missing = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "go"},
        ["TestA", "TestMissing"],
        0,
        go_log,
    )
    assert missing["ok"] is False
    assert missing["missing"] == ["TestMissing"]


def test_test_patch_bindings_preserve_a_literal_b_directory_segment(tmp_path):
    namespace = _remote_namespace(tmp_path)
    python_path = "tests/x b/test_widget.py"
    python_patch = (
        f"diff --git a/{python_path} b/{python_path}\n"
        f"--- a/{python_path}\t\n"
        f"+++ b/{python_path}\t\n"
        "@@ -0,0 +1 @@\n+import requests.sessions\n"
    )
    assert namespace["_python_test_patch_import_bindings"](
        {"repo": "psf/requests", "test_patch": python_patch},
        [f"{python_path}::test_widget"],
    ) == [{"test_file": python_path, "modules": ["requests.sessions"]}]

    javascript_path = "applications/x b/widget.test.js"
    javascript_patch = (
        f"diff --git a/{javascript_path} b/{javascript_path}\n"
        f"--- a/{javascript_path}\t\n"
        f"+++ b/{javascript_path}\t\n"
        "@@ -0,0 +1 @@\n+jest.mock('@proton/shared/lib/widget')\n"
    )
    assert namespace["_js_suite_module_mock_bindings"](
        {"repo": "protonmail/webclients", "test_patch": javascript_patch},
        [javascript_path],
    ) == [{"suite": javascript_path, "modules": ["@proton/shared/lib/widget"]}]


def test_go_test_command_discovers_each_named_test_package(tmp_path):
    namespace = _command_namespace()
    for package, test_name in (("pkg/a", "TestA"), ("pkg/b", "TestB")):
        path = tmp_path / package
        path.mkdir(parents=True)
        (path / "feature_test.go").write_text(
            f"package feature\nfunc {test_name}(t *testing.T) {{}}\n", encoding="utf-8"
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$GO_CALLS\"\n"
        "case \"$*\" in\n"
        "  *./pkg/a*) printf '%s\\n' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/a\",\"Test\":\"TestA\"}' ;;\n"
        "  *) printf '%s\\n' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/b\",\"Test\":\"TestB/subcase\"}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    calls = tmp_path / "go.calls"
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["GO_CALLS"] = str(calls)

    command = namespace["go_test_command"](["TestA", "TestB/subcase"])
    proc = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0
    assert proc.stdout.count("OPENCOLLAB_GO_TARGET_DISCOVERY ") == 2
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "test -count=1 -json ./pkg/a -run ^TestA$",
        "test -count=1 -json ./pkg/b -run ^TestB$/^subcase$",
    ]


def test_go_test_command_requires_declared_complex_events_and_preserves_root_failure(tmp_path):
    namespace = _command_namespace()
    package = tmp_path / "pkg/a"
    package.mkdir(parents=True)
    (package / "feature_test.go").write_text(
        "package feature\nfunc TestPaths(t *testing.T) {}\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$GO_CALLS\"\n"
        "printf '%s\\n' "
        "'{\"Action\":\"fail\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths\"}' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths/#00\"}' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths//api\"}' "
        "'{\"Action\":\"fail\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths/unselected\"}'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    calls = tmp_path / "go.calls"
    env["GO_CALLS"] = str(calls)
    targets = ["TestPaths", "TestPaths/#00", "TestPaths//api"]

    proc = subprocess.run(
        ["bash", "-c", namespace["go_test_command"](targets)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1
    marker = next(
        line for line in proc.stdout.splitlines() if line.startswith("OPENCOLLAB_GO_TARGET_DISCOVERY ")
    )
    assert json.loads(marker.split(" ", 1)[1])["tests"] == targets
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "test -count=1 -json ./pkg/a -run ^TestPaths$"
    ]


def test_go_test_command_fails_when_a_declared_event_is_missing(tmp_path):
    namespace = _command_namespace()
    package = tmp_path / "pkg/a"
    package.mkdir(parents=True)
    (package / "feature_test.go").write_text(
        "package feature\nfunc TestPaths(t *testing.T) {}\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths/#00\"}'\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]

    proc = subprocess.run(
        ["bash", "-c", namespace["go_test_command"](["TestPaths/#00", "TestPaths//api"])],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1


def test_go_test_command_preserves_go_failure_after_declared_pass(tmp_path):
    namespace = _command_namespace()
    package = tmp_path / "pkg/a"
    package.mkdir(parents=True)
    (package / "feature_test.go").write_text(
        "package feature\nfunc TestPaths(t *testing.T) {}\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "'{\"Action\":\"pass\",\"Package\":\"example/pkg/a\",\"Test\":\"TestPaths/#00\"}'\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]

    proc = subprocess.run(
        ["bash", "-c", namespace["go_test_command"](["TestPaths/#00"])],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2


def test_go_test_command_runs_every_package_with_the_same_named_test(tmp_path):
    namespace = _command_namespace()
    for package in ("pkg/a", "pkg/b"):
        path = tmp_path / package
        path.mkdir(parents=True)
        (path / "feature_test.go").write_text(
            "package feature\nfunc TestSame(t *testing.T) {}\n",
            encoding="utf-8",
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        """#!/bin/sh
package=${4#./}
printf '{"Action":"pass","Package":"example.org/project/%s","Test":"TestSame"}\n' "$package"
exit 0
""",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]

    command = namespace["go_test_command"](["TestSame"])
    proc = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout.count("OPENCOLLAB_GO_TARGET_DISCOVERY") == 2
    assert '"Package":"example.org/project/pkg/a"' in proc.stdout
    assert '"Package":"example.org/project/pkg/b"' in proc.stdout


def test_go_plan_discovers_packages_for_bare_test_names():
    namespace = _command_namespace()

    plan = namespace["prolite_test_plan"](
        {"repo_language": "go", "repo": "gravitational/teleport"},
        ["TestColumn", "TestMessage"],
    )

    assert plan["adapter"] == "go-test-json-discovery"
    assert plan["coverage_verified"] is True
    assert plan["target_batches"] == [["TestColumn", "TestMessage"]]
    assert len(plan["commands"]) == 1
    assert plan["commands"][0].startswith("python3 -I -c ")
    assert "unable to map Go tests to packages" in plan["commands"][0]
    assert plan["proofs"] == [
        {
            "kind": "go_json_test_pass",
            "tests": ["TestColumn", "TestMessage"],
            "dynamic_discovery": True,
        }
    ]


def test_ansible_test_command_forces_repository_import_root():
    namespace = _command_namespace()

    command = namespace["ansible_python_test_command"](
        ["test/units/galaxy/test_api.py::test_target"]
    )

    assert 'export PYTHONPATH="$PWD/lib${PYTHONPATH:+:$PYTHONPATH}"' in command
    assert "wrong ansible import root" in command
    assert "python3 -m pytest -vv test/units/galaxy/test_api.py::test_target" in command


def test_python_test_targets_are_batched_without_file_level_expansion():
    namespace = _command_namespace()
    targets = [f"tests/test_many.py::test_case[{index}]" for index in range(149)]

    compacted = namespace["compact_python_test_targets"](targets, [])
    command = namespace["python_test_command"](compacted)

    assert compacted == targets
    assert command.count("python3 -m pytest -vv") == 4
    assert "tests/test_many.py::test_case[0]" in command
    assert "tests/test_many.py::test_case[148]" in command
    malformed = [
        "tests/test_many.py::test_case[param-a",
        "tests/test_many.py::test_case[param-b",
    ]
    assert namespace["compact_python_test_targets"](malformed, []) == [
        "tests/test_many.py::test_case"
    ]


def test_python_parameter_targets_use_external_parent_bound_proof():
    namespace = _command_namespace()
    targets = [
        "tests/test_many.py::test_case[dataset-repr-a]",
        "tests/test_many.py::test_case[dataset-repr-b]",
        "tests/test_many.py::test_stable",
    ]

    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, targets)

    assert plan["coverage_verified"] is True
    assert plan["coverage"] == "parameter_parent_targets"
    assert plan["commands"] == [
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_many.py::test_case tests/test_many.py::test_stable"
    ]
    assert plan["proofs"][0]["parameter_fallback_parents"] == [
        "tests/test_many.py::test_case"
    ]
    assert len(plan["proofs"][0]["command_sha256"]) == 64


def test_python_batch_command_keeps_targets_out_of_bash_argv(tmp_path):
    namespace = _command_namespace()

    command = namespace["python_batch_test_command"](
        "/eval_input/p2p.targets.json", "qutebrowser/qutebrowser"
    )

    assert len(command) < 3000
    assert "/eval_input/p2p.targets.json" in command
    assert "xvfb-run" in command
    assert '"--no-xvfb"' in command
    assert '"no:xvfb"' not in command
    assert "targets[offset:offset + 40]" in command

    test_file = tmp_path / "test_param.py"
    test_file.write_text(
        "import pytest\n@pytest.mark.parametrize('value', ['alpha', 'beta'])\n"
        "def test_case(value):\n    assert value\n",
        encoding="utf-8",
    )
    targets_file = tmp_path / "targets.json"
    targets_file.write_text(json.dumps(["test_param.py::test_case[alpha"]), encoding="utf-8")
    executable = namespace["python_batch_test_command"](str(targets_file), "example/repo")
    proc = subprocess.run(
        ["bash", "-c", executable],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", ""),
        },
    )
    assert proc.returncode == 0
    assert "test_param.py::test_case[alpha] PASSED" in proc.stdout
    assert "test_param.py::test_case[beta] PASSED" in proc.stdout


def test_python_proof_preserves_passes_across_partial_batch_failure():
    namespace = _proof_namespace()
    expected = [
        "tests/test_feature.py::test_one",
        "tests/test_feature.py::test_two",
        "tests/test_feature.py::test_value[Hello World ☃]",
        "tests/test_feature.py::test_never_started",
    ]
    log = "\n".join(
        [
            "tests/test_feature.py::test_one PASSED [ 50%]",
            "XIO:  fatal IO error 11 (Resource temporarily unavailable)",
            "tests/test_feature.py::test_two PASSED [100%]",
            "tests/test_feature.py::test_value[Hello World ☃] PASSED [100%]",
            "tests/test_feature.py::test_value[Hello World ☃] FAILED [100%]",
        ]
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "qutebrowser/qutebrowser"},
        expected,
        1,
        log,
    )

    assert proof["passed"] == expected[:2]
    assert proof["failed"] == [expected[2]]
    assert proof["missing"] == [expected[3]]
    assert proof["ok"] is False
    assert namespace["eval_log_has_infra_failure"](1, log) is False

    malformed_expected = ["tests/test_feature.py::test_param[value with newline"]
    malformed_log = "\n".join(
        [
            "tests/test_feature.py::test_param[value one] PASSED [ 50%]",
            "tests/test_feature.py::test_param[value two] PASSED [100%]",
        ]
    )
    malformed_proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "example/repo"},
        malformed_expected,
        0,
        malformed_log,
    )
    assert malformed_proof["passed"] == malformed_expected
    failing_family = malformed_log + "\ntests/test_feature.py::test_param[value three] FAILED [100%]"
    failing_proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "example/repo"},
        malformed_expected,
        1,
        failing_family,
    )
    assert failing_proof["passed"] == []
    assert failing_proof["failed"] == malformed_expected


def test_eval_only_identity_recomputes_full_patch_sha():
    namespace = {
        "row_patch_sha": swe_eval_records.row_patch_sha,
        "completed_artifact_identity_matches": (
            remote_records.completed_generation_identity
        ),
    }
    task = "instance_owner__repo-1"
    patch = "diff --git a/a b/a\n"
    computed = hashlib.sha256(patch.encode()).hexdigest()
    prediction = {
        "instance_id": task,
        "record_id": "record",
        "model_patch": patch,
        "patch_sha256": computed,
    }
    metric = {
        "instance_id": task,
        "record_id": "record",
        "patch_sha256": computed,
        "workflow_status": "done",
        "runner_returncode": 0,
    }

    assert namespace["row_patch_sha"](prediction) == computed
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is False
    assert namespace["completed_artifact_identity_matches"](
        prediction,
        metric,
        task,
        require_submission_integrity=False,
    ) is True
    prediction["patch_sha256"] = "a" * 64
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is False
    prediction["patch_sha256"] = computed[:12]
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is False


def test_patch_fallback_ignores_crlf_context_for_benchmark_test_patch(tmp_path):
    function = _patch_fallback_function()
    (tmp_path / "file.txt").write_bytes(b"left\r\n")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1,2 @@",
                " left",
                "+right",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file} ignore-space-change\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode == 0
    assert b"right" in (tmp_path / "file.txt").read_bytes()


def test_patch_fallback_dry_run_prevents_partial_application(tmp_path):
    function = _patch_fallback_function()
    (tmp_path / "first.txt").write_text("first-old\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second-different\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/first.txt b/first.txt",
                "--- a/first.txt",
                "+++ b/first.txt",
                "@@ -1 +1 @@",
                "-first-old",
                "+first-new",
                "diff --git a/second.txt b/second.txt",
                "--- a/second.txt",
                "+++ b/second.txt",
                "@@ -1 +1 @@",
                "-second-old",
                "+second-new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode != 0
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first-old\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second-different\n"
