"""Positive execution proof for ProLite language adapters."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest


def _pytest_proof_text(nodeids, *, exitstatus=0, call_outcome="passed"):
    events = [
        {"event": "session_start"},
        {"event": "collection_finish", "nodeids": list(nodeids)},
    ]
    for nodeid in nodeids:
        for phase in ("setup", "call", "teardown"):
            events.append(
                {
                    "event": "runtest_logreport",
                    "nodeid": nodeid,
                    "when": phase,
                    "outcome": call_outcome if phase == "call" else "passed",
                }
            )
    events.append({"event": "session_finish", "exitstatus": exitstatus})
    return "".join(json.dumps(event) + "\n" for event in events)


def test_eval_runner_dependency_failures_are_infrastructure(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["eval_log_has_infra_failure"](127, "No supported JS test runner found for jest") is True
    assert namespace["eval_log_has_infra_failure"](124, "test command timed out") is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected error message 'request timed out'",
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1,
        "redis.exceptions.ConnectionError: Connection refused",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "MongoDB server unavailable: failed to connect",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected 'Connection refused' but got 'accepted'",
    ) is False


def test_prolite_go_command_requires_exact_test_targets_and_json_events(tmp_path):
    namespace = _remote_namespace(tmp_path)

    plan = namespace["prolite_test_plan"](
        {"repo_language": "go"},
        [
            "internal/api/widget_test.go::TestWidget",
            "pkg/server/router_test.go::TestRouter/subcase",
        ],
    )

    assert plan["coverage_verified"] is True
    assert plan["coverage"] == "exact_test_events"
    assert plan["commands"] == [
        "go test -count=1 -json ./internal/api -run '^TestWidget$'",
        "go test -count=1 -json ./pkg/server -run '^TestRouter/subcase$'",
    ]
    assert plan["proofs"] == [
        {
            "kind": "go_json_test_pass",
            "test": "TestWidget",
            "package": "./internal/api",
            "test_file": "internal/api/widget_test.go",
        },
        {
            "kind": "go_json_test_pass",
            "test": "TestRouter/subcase",
            "package": "./pkg/server",
            "test_file": "pkg/server/router_test.go",
        },
    ]


def test_prolite_go_file_only_target_is_technical_red(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "go"},
        ["internal/api/widget_test.go"],
    )

    assert plan["coverage_verified"] is False
    assert plan["commands"] == []


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_prolite_js_file_target_uses_positive_event_parser(
    tmp_path,
    language,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": language},
        ["test/widget.test.js"],
    )

    assert plan["coverage_verified"] is True
    assert plan["adapter"] == "jest-json-verbose"
    assert "--json" in plan["commands"][0]
    assert plan["proofs"] == [
        {
            "kind": "js_parser_backed_targets",
            "targets": ["test/widget.test.js"],
            "repo_language": language,
            "repo": "",
        }
    ]


def test_prolite_go_log_proof_rejects_package_pass_without_test_event(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {"kind": "go_json_test_pass", "test": "TestWidget"}

    assert namespace["_plan_log_proof_matches"](
        proof,
        '{"Action":"pass","Package":"example/internal/api"}\n',
    ) is False
    assert namespace["_plan_log_proof_matches"](proof, "[no test files]\n") is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        '{"Action":"run","Test":"TestWidget"}\n'
        '{"Action":"pass","Test":"TestWidget"}\n',
    ) is True


def test_prolite_go_log_proof_requires_every_discovered_target(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestColumn", "TestMessage"],
    }
    complete = (
        '{"Action":"pass","Test":"TestColumn"}\n'
        '{"Action":"pass","Test":"TestMessage"}\n'
    )
    partial = '{"Action":"pass","Test":"TestColumn"}\n'

    assert namespace["_plan_log_proof_matches"](proof, complete) is True
    assert namespace["_plan_log_proof_matches"](proof, partial) is False


def _go_build_failure_log(
    *,
    package: str = "example.org/project/internal/api",
    test_file: str = "internal/api/widget_test.go",
) -> str:
    return "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "ImportPath": package + " [" + package + ".test]",
                "Action": "build-output",
                "Output": f"{test_file}:42:7: undefined: missingSymbol\n",
            },
            {
                "ImportPath": package + " [" + package + ".test]",
                "Action": "build-fail",
            },
            {
                "Action": "output",
                "Package": package,
                "Output": f"FAIL\t{package} [build failed]\n",
            },
            {"Action": "fail", "Package": package, "Elapsed": 0.01},
        )
    )


def test_prolite_go_build_failure_proof_binds_package_and_target_test_file(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "test": "TestWidget",
        "package": "./internal/api",
        "test_file": "internal/api/widget_test.go",
    }

    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_build_failure_log(),
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_build_failure_log(test_file="internal/api/unrelated_test.go"),
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_build_failure_log(package="example.org/project/other"),
    ) is False
    extra_package = _go_build_failure_log() + json.dumps(
        {"Action": "fail", "Package": "example.org/project/other"}
    )
    assert namespace["_plan_log_failure_proof_matches"](proof, extra_package) is False


def test_prolite_go_build_failure_rejects_incomplete_or_unstructured_logs(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "test": "TestWidget",
        "package": "./internal/api",
        "test_file": "internal/api/widget_test.go",
    }
    no_build_marker = _go_build_failure_log().replace(" [build failed]", "")
    no_package_fail = "".join(_go_build_failure_log().splitlines(keepends=True)[:-1])
    raw_compile_error = (
        "internal/api/widget_test.go:42:7: undefined: missingSymbol\n"
        "FAIL\texample.org/project/internal/api [build failed]\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](proof, no_build_marker) is False
    assert namespace["_plan_log_failure_proof_matches"](proof, no_package_fail) is False
    assert namespace["_plan_log_failure_proof_matches"](proof, raw_compile_error) is False


def test_prolite_go_dynamic_build_failure_requires_unique_discovery_binding(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestWal2JSON"],
        "dynamic_discovery": True,
    }
    marker = "OPENCOLLAB_GO_TARGET_DISCOVERY " + json.dumps(
        {
            "package": "./lib/srv",
            "tests": ["TestWal2JSON"],
            "test_files": ["lib/srv/wal2json_test.go"],
        },
        sort_keys=True,
    )
    log = marker + "\n" + _go_build_failure_log(
        package="github.com/gravitational/teleport/lib/srv",
        test_file="lib/srv/wal2json_test.go",
    )

    assert namespace["_plan_log_failure_proof_matches"](proof, log) is True
    duplicate = (
        marker
        + "\n"
        + marker.replace("./lib/srv", "./lib/other")
        + "\n"
        + _go_build_failure_log(
            package="github.com/gravitational/teleport/lib/srv",
            test_file="lib/srv/wal2json_test.go",
        )
    )
    assert namespace["_plan_log_failure_proof_matches"](proof, duplicate) is False


def _go_dynamic_two_package_log(*, action: str, swap_packages: bool) -> str:
    markers = [
        {
            "package": "./pkg/a",
            "tests": ["TestA"],
            "test_files": ["pkg/a/a_test.go"],
        },
        {
            "package": "./pkg/b",
            "tests": ["TestB"],
            "test_files": ["pkg/b/b_test.go"],
        },
    ]
    owners = {
        "TestA": "example.org/project/pkg/a",
        "TestB": "example.org/project/pkg/b",
    }
    if swap_packages:
        owners = {"TestA": owners["TestB"], "TestB": owners["TestA"]}
    return "".join(
        [
            *(
                "OPENCOLLAB_GO_TARGET_DISCOVERY "
                + json.dumps(marker, sort_keys=True)
                + "\n"
                for marker in markers
            ),
            *(
                json.dumps(
                    {"Action": action, "Package": owners[test], "Test": test}
                )
                + "\n"
                for test in ("TestA", "TestB")
            ),
        ]
    )


def test_prolite_go_dynamic_pass_binds_each_test_to_unique_owner_package(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestA", "TestB"],
        "dynamic_discovery": True,
    }

    assert namespace["_plan_log_proof_matches"](
        proof,
        _go_dynamic_two_package_log(action="pass", swap_packages=False),
    ) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        _go_dynamic_two_package_log(action="pass", swap_packages=True),
    ) is False


def test_prolite_go_dynamic_failure_binds_each_test_to_unique_owner_package(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestA", "TestB"],
        "dynamic_discovery": True,
    }

    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_dynamic_two_package_log(action="fail", swap_packages=False),
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_dynamic_two_package_log(action="fail", swap_packages=True),
    ) is False


def test_go_build_failure_execution_evidence_still_requires_exact_plan_command(tmp_path):
    namespace = _remote_namespace(tmp_path)
    f2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "go"},
        ["internal/api/widget_test.go::TestWidget"],
    )
    p2p_plan = namespace["prolite_test_plan"]({"repo_language": "go"}, [])
    for name, value in {
        "base_commit.exit": "0\n",
        "service_bootstrap.exit": "0\n",
        "before_repo.exit": "0\n",
        "post_before_base.exit": "0\n",
        "model_patch.exit": "0\n",
        "test_patch.exit": "0\n",
        "f2p.exit": "1\n",
        "p2p.exit": "0\n",
        "f2p.log": "",
        "p2p.log": "",
        "f2p.command": f2p_plan["commands"][0] + "\n",
        "p2p.command": "",
        "service_bootstrap.log": "",
        "base_commit.log": "",
        "before_repo.log": "",
        "model_patch.log": "",
        "test_patch.log": "",
        "f2p.batch_001.exit": "1\n",
        "f2p.batch_001.command": f2p_plan["commands"][0] + "\n",
        "f2p.batch_001.log": _go_build_failure_log(),
    }.items():
        (tmp_path / name).write_text(value, encoding="utf-8")

    trusted = namespace["read_eval_output_artifacts"](
        tmp_path,
        f2p_plan,
        p2p_plan,
        "nonce",
    )

    assert trusted["f2p_execution_evidence_complete"] is True
    assert trusted["f2p_evidence"][0]["command_matches_plan"] is True
    assert trusted["f2p_evidence"][0]["target_failure_proof_matches_plan"] is True

    (tmp_path / "f2p.batch_001.command").write_text(
        "go test -count=1 -json ./other -run '^TestWidget$'\n",
        encoding="utf-8",
    )
    tampered = namespace["read_eval_output_artifacts"](
        tmp_path,
        f2p_plan,
        p2p_plan,
        "nonce",
    )

    assert tampered["f2p_execution_evidence_complete"] is False
    assert tampered["f2p_evidence"][0]["command_matches_plan"] is False


def test_task76_legacy_discovery_build_failure_is_bound_to_matched_command(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {"kind": "go_json_test_pass", "tests": ["TestScanner"]}
    old_command = r'''python3 -c 'import json
import pathlib
import re
import subprocess
names = json.loads('["TestScanner"]')
for path in pathlib.Path(".").rglob("*_test.go"):
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?m)^func\s+" + re.escape(name) + r"\s*\(", text):
        pass
print("unable to map Go tests to packages: " + "")
subprocess.run(["go", "test", "-count=1", "-json", package, "-run", pattern])
' '''
    task76_log = "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "ImportPath": (
                    "github.com/navidrome/navidrome/scanner "
                    "[github.com/navidrome/navidrome/scanner.test]"
                ),
                "Action": "build-output",
                "Output": (
                    "scanner/walk_dir_tree_test.go:21:20: "
                    "undefined: walkResults\n"
                ),
            },
            {
                "ImportPath": (
                    "github.com/navidrome/navidrome/scanner "
                    "[github.com/navidrome/navidrome/scanner.test]"
                ),
                "Action": "build-fail",
            },
            {
                "Action": "output",
                "Package": "github.com/navidrome/navidrome/scanner",
                "Output": (
                    "FAIL\tgithub.com/navidrome/navidrome/scanner "
                    "[build failed]\n"
                ),
            },
            {
                "Action": "fail",
                "Package": "github.com/navidrome/navidrome/scanner",
                "FailedBuild": (
                    "github.com/navidrome/navidrome/scanner "
                    "[github.com/navidrome/navidrome/scanner.test]"
                ),
            },
        )
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        task76_log,
        "",
        old_command,
        old_command,
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        task76_log,
        "",
        old_command,
        old_command + " # changed",
    ) is False


def test_prolite_test_command_never_falls_back_to_a_passing_noop(tmp_path):
    namespace = _remote_namespace(tmp_path)
    command = namespace["prolite_test_command"]
    is_runnable = namespace["_is_runnable_test_command"]

    assert command({"repo_language": "python"}, []) == ""
    assert command({}, []) == ""
    assert command({"repo_language": "ruby"}, ["spec/widget_spec.rb"]) == ""
    assert command(
        {"repo_language": "ruby", "test_cmd": "echo ok", "eval_cmd": "echo also-ok"},
        ["spec/widget_spec.rb"],
    ) == ""
    assert not is_runnable("")
    assert not is_runnable("true")
    assert not is_runnable(" : ")
    assert not is_runnable("echo ok")
    assert is_runnable(
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_x.py::test_y"
    )


def test_prolite_pytest_console_ignores_shadow_module_and_collect_only_addopts(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "pytest.py").write_text(
        "print('shadow pytest: no tests')\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --collect-only\n",
        encoding="utf-8",
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"

    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "shadow pytest" not in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is True


def test_prolite_pytest_proof_rejects_conftest_exit_status_rewrite(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert False\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"

    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert f"PASSED {target}" in log
    assert f"FAILED {target}" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is False


def test_prolite_pytest_proof_rejects_cleared_collection_with_forged_pass(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items.clear()\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"
    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))

    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "no tests ran" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is False


def test_prolite_pytest_proof_rejects_forged_pass_line_and_summary(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    proof = {"kind": "pytest_structured_reports", "targets": [target]}
    forged_failure = (
        f"PASSED {target}\n"
        f"FAILED {target} - AssertionError\n"
        "1 failed in 0.01s\n"
        "1 passed in 0.01s\n"
    )
    forged_empty = f"PASSED {target}\n1 passed in 0.01s\nno tests ran in 0.00s\n"

    structured = _pytest_proof_text([target])
    assert namespace["_plan_log_proof_matches"](proof, forged_failure, structured) is False
    assert namespace["_plan_log_proof_matches"](proof, forged_empty, structured) is False


def test_prolite_model_patch_filters_pytest_conftest_changes(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/conftest.py b/conftest.py\n"
        "--- a/conftest.py\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1 @@\n"
        "+def pytest_sessionfinish(session): session.exitstatus = 0\n"
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "conftest.py" not in filtered
    assert "src/widget.py" in filtered
