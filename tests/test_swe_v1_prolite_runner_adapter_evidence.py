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


def test_task9_jest_suite_load_failure_binds_official_mock_and_exact_command(tmp_path):
    namespace = _remote_namespace(tmp_path)
    suite = (
        "packages/components/components/drawer/views/SecurityCenter/PassAliases/"
        "PassAliases.test.tsx"
    )
    declared_suite = suite.removeprefix("packages/components/")
    missing_module = (
        "@proton/components/components/drawer/views/SecurityCenter/PassAliases/"
        "usePassAliasesProviderSetup"
    )
    target = declared_suite + " | PassAliases renders the aliases list"
    test_patch = (
        f"diff --git a/{suite} b/{suite}\n"
        f"--- a/{suite}\n"
        f"+++ b/{suite}\n"
        "@@ -1 +1,2 @@\n"
        "+jest.mock('"
        + missing_module
        + "', () => ({ usePassAliasesSetup: () => {} }));\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "js",
            "repo": "protonmail/webclients",
            "selected_test_files_to_run": json.dumps([suite]),
            "test_patch": test_patch,
        },
        [target],
    )
    proof = plan["proofs"][0]
    command = plan["commands"][0]
    result = {
        "numFailedTestSuites": 1,
        "numRuntimeErrorTestSuites": 1,
        "numTotalTestSuites": 1,
        "numTotalTests": 0,
        "success": False,
        "testResults": [
            {
                "assertionResults": [],
                "name": "/app/" + suite,
                "status": "failed",
                "message": (
                    "  ● Test suite failed to run\n\n"
                    f"    Cannot find module '{missing_module}' from '{declared_suite}'\n"
                ),
            }
        ],
    }
    log = f"FAIL {suite}\n" + json.dumps(result) + "\n"

    assert proof["suite_module_mocks"] == [
        {"suite": suite, "modules": [missing_module]}
    ]
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
    rejected = (
        log.replace(missing_module, "@proton/components/unbound", 1),
        log.replace(declared_suite, "components/other.test.tsx", 1),
        log.replace("numRuntimeErrorTestSuites\": 1", "numRuntimeErrorTestSuites\": 0"),
        log.replace("Cannot find module", "Validation Error"),
        log.replace("FAIL " + suite, "FAIL packages/components/other.test.tsx"),
    )
    for bad_log in rejected:
        assert namespace["_plan_log_failure_proof_matches"](
            proof, bad_log, "", command, command
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command + " changed"
    ) is False


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


def test_prolite_go_build_failure_proof_binds_package_and_test_compilation_unit(tmp_path):
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
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        _go_build_failure_log(test_file="internal/other/unrelated_test.go"),
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
    command = "exact dynamic discovery command"

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
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
    assert namespace["_plan_log_failure_proof_matches"](
        proof, duplicate, "", command, command
    ) is False


def test_prolite_go_mixed_plain_build_failure_binds_command_package_and_file(tmp_path):
    namespace = _remote_namespace(tmp_path)
    package = "example.org/project/internal/api"
    marker = "OPENCOLLAB_GO_TARGET_DISCOVERY " + json.dumps(
        {
            "package": "./internal/api",
            "tests": ["TestWidget"],
            "test_files": ["internal/api/widget_test.go"],
        },
        sort_keys=True,
    )
    events = "".join(
        json.dumps(event) + "\n"
        for event in (
            {"Action": "start", "Package": package},
            {
                "Action": "output",
                "Package": package,
                "Output": f"FAIL\t{package} [build failed]\n",
            },
            {"Action": "fail", "Package": package},
        )
    )
    log = (
        marker
        + f"\n# {package} [{package}.test]\n"
        + "internal/api/widget_test.go:42:7: undefined: missingSymbol\n"
        + events
    )
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestWidget"],
        "dynamic_discovery": True,
    }
    command = "exact dynamic Go command"

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command + " changed"
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace("internal/api/widget_test.go:42:7", "internal/api/other_test.go:42:7"),
        "",
        command,
        command,
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace("internal/api/widget_test.go:42:7", "internal/other/other_test.go:42:7"),
        "",
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace(f"# {package} [{package}.test]", "# example.org/wrong [example.org/wrong.test]"),
        "",
        command,
        command,
    ) is False
    extra_failure = log + json.dumps(
        {"Action": "fail", "Package": "example.org/project/other"}
    )
    assert namespace["_plan_log_failure_proof_matches"](
        proof, extra_failure, "", command, command
    ) is False


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


def test_task76_fresh_discovery_accepts_same_package_test_compile_error(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestScanner"],
        "dynamic_discovery": True,
    }
    marker = "OPENCOLLAB_GO_TARGET_DISCOVERY " + json.dumps(
        {
            "package": "./scanner",
            "tests": ["TestScanner"],
            "test_files": ["scanner/scanner_suite_test.go"],
        },
        sort_keys=True,
    )
    command = "exact fresh discovery command"
    log = marker + "\n" + _go_build_failure_log(
        package="github.com/navidrome/navidrome/scanner",
        test_file="scanner/walk_dir_tree_test.go",
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command + " changed"
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace("scanner/walk_dir_tree_test.go", "other/walk_dir_tree_test.go"),
        "",
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace(
            "github.com/navidrome/navidrome/scanner",
            "github.com/navidrome/navidrome/other",
        ),
        "",
        command,
        command,
    ) is False


def test_task97_dynamic_build_failure_proves_each_failed_package(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {
        "kind": "go_json_test_pass",
        "tests": ["TestAI", "TestModel"],
        "dynamic_discovery": True,
    }
    packages = (
        ("./lib/ai", "TestAI", "lib/ai/ai_test.go"),
        ("./lib/ai/model", "TestModel", "lib/ai/model/model_test.go"),
    )
    parts = []
    for package, test, test_file in packages:
        import_path = "example.org/project/" + package.removeprefix("./")
        parts.append(
            "OPENCOLLAB_GO_TARGET_DISCOVERY "
            + json.dumps(
                {"package": package, "tests": [test], "test_files": [test_file]},
                sort_keys=True,
            )
            + "\n"
        )
        parts.append(f"# {import_path} [{import_path}.test]\n")
        parts.append(f"{test_file}:21:7: undefined: missingSymbol\n")
        parts.append(
            json.dumps(
                {
                    "Action": "output",
                    "Package": import_path,
                    "Output": f"FAIL\t{import_path} [build failed]\n",
                }
            )
            + "\n"
        )
        parts.append(json.dumps({"Action": "fail", "Package": import_path}) + "\n")
    log = "".join(parts)
    command = "exact two-package discovery command"

    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace("lib/ai/ai_test.go:21", "lib/ai/model/ai_test.go:21", 1),
        "",
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace(
            "# example.org/project/lib/ai/model [example.org/project/lib/ai/model.test]",
            "# example.org/project/lib/wrong [example.org/project/lib/wrong.test]",
        ),
        "",
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log + json.dumps({"Action": "fail", "Package": "example.org/project/other"}),
        "",
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof, log, "", command, command + " changed"
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


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        ("def test_target():\n    assert True\n", 0),
        ("def test_target():\n    assert False\n", 1),
        ("import opencollab_missing_production_module\n", 4),
    ],
)
def test_prolite_pytest_proof_is_host_readable_after_session_finish(
    tmp_path,
    source,
    expected_status,
):
    namespace = _remote_namespace(tmp_path)
    (tmp_path / "test_target.py").write_text(source, encoding="utf-8")
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"

    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("pytest")),
            "-p",
            "opencollab_pytest_proof",
            "-q",
            "-o",
            "addopts=",
            "test_target.py::test_target",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_status
    assert proof_path.stat().st_mode & 0o777 == 0o644
    events = [json.loads(line) for line in proof_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1] == {"event": "session_finish", "exitstatus": expected_status}


@pytest.mark.parametrize("existing_kind", ["regular", "symlink"])
def test_prolite_pytest_proof_keeps_exclusive_nofollow_creation(
    tmp_path,
    monkeypatch,
    existing_kind,
):
    namespace = _remote_namespace(tmp_path)
    source = namespace["prolite_pytest_proof_plugin_source"]()
    victim = tmp_path / "victim.jsonl"
    victim.write_text("sentinel\n", encoding="utf-8")
    proof_path = tmp_path / "proof.jsonl"
    if existing_kind == "regular":
        proof_path.write_text("existing\n", encoding="utf-8")
    else:
        proof_path.symlink_to(victim)
    monkeypatch.setenv("OPENCOLLAB_PYTEST_PROOF_PATH", str(proof_path))
    plugin = {}
    exec(source, plugin)

    with pytest.raises(OSError):
        plugin["pytest_sessionstart"](None)

    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    if existing_kind == "regular":
        assert proof_path.read_text(encoding="utf-8") == "existing\n"


def test_prolite_pytest_proof_remains_private_before_session_finish(
    tmp_path,
    monkeypatch,
):
    namespace = _remote_namespace(tmp_path)
    proof_path = tmp_path / "proof.jsonl"
    monkeypatch.setenv("OPENCOLLAB_PYTEST_PROOF_PATH", str(proof_path))
    plugin = {}
    exec(namespace["prolite_pytest_proof_plugin_source"](), plugin)

    plugin["pytest_sessionstart"](None)

    assert proof_path.stat().st_mode & 0o777 == 0o600
    assert namespace["_plan_log_proof_matches"](
        {"kind": "pytest_structured_reports", "targets": ["test_target.py::test_target"]},
        "",
        proof_path.read_text(encoding="utf-8"),
    ) is False
    os.close(plugin["_fd"])


def test_prolite_pytest_collection_import_failure_is_exact_semantic_failure(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target_file = "openlibrary/plugins/worksearch/schemes/tests/test_works.py"
    target = target_file + "::test_process_user_query"
    module = "openlibrary.plugins.worksearch.schemes.works"
    test_patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{target_file}\n"
        "@@ -0,0 +1,2 @@\n"
        f"+from {module} import WorkSearchScheme\n"
        "+def test_process_user_query(): pass\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "python",
            "repo": "internetarchive/openlibrary",
            "test_patch": test_patch,
        },
        [target],
    )
    proof = plan["proofs"][0]
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = plan["commands"][0]
    valid_log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:2: in <module>\n"
        f"    from {module} import WorkSearchScheme\n"
        f"E   ModuleNotFoundError: No module named '{module}'\n"
    )

    assert proof["target_imports"] == [
        {"test_file": target_file, "modules": [module]}
    ]
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log,
        proof_text,
        command,
        command,
    ) is True
    invalid_logs = (
        "no tests ran in 0.01s\n",
        valid_log.replace(module, "openlibrary.plugins.unbound", 1),
        valid_log.replace("ERROR collecting " + target_file, "ERROR collecting tests/test_other.py"),
        valid_log.replace("from " + module, "from openlibrary.plugins.unbound"),
    )
    for log in invalid_logs:
        assert namespace["_plan_log_failure_proof_matches"](
            proof,
            log,
            proof_text,
            command,
            command,
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log,
        proof_text,
        command,
        command + " # changed",
    ) is False


def test_prolite_pytest_collection_rejects_unbound_third_party_import(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target_file = "tests/test_target.py"
    target = target_file + "::test_target"
    test_patch = (
        f"diff --git a/{target_file} b/{target_file}\n"
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@ -0,0 +1 @@\n"
        "+import numpy\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "python",
            "repo": "example/repo",
            "test_patch": test_patch,
        },
        [target],
        candidate_source_paths=["src/candidate.py"],
    )
    proof = plan["proofs"][0]
    proof_text = _pytest_proof_text([], exitstatus=4)
    log = (
        f"ERROR collecting {target_file}\n"
        f"{target_file}:1: in <module>\n"
        "    import numpy\n"
        "E   ModuleNotFoundError: No module named 'numpy'\n"
    )

    assert "target_imports" not in proof
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log,
        proof_text,
        plan["commands"][0],
        plan["commands"][0],
    ) is False


def test_prolite_pytest_collection_candidate_exception_binds_modified_source(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "tests/unit/keyinput/test_keyutils.py::test_target[param]"
    source_path = "qutebrowser/keyinput/keyutils.py"
    proof = {
        "kind": "pytest_structured_reports",
        "targets": [target],
        "parameter_fallback_parents": [target.split("[", 1)[0]],
        "candidate_source_paths": [source_path],
    }
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = (
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/unit/keyinput/test_keyutils.py::test_target"
    )
    valid_log = (
        "ERROR collecting tests/unit/keyinput/test_keyutils.py\n"
        "tests/unit/keyinput/test_keyutils.py:247: in <module>\n"
        f"{source_path}:501: in _convert_key\n"
        "E   AssertionError: <Ctrl+Alt+y>\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command
    ) is True
    rejected_logs = (
        valid_log.replace("tests/unit/keyinput/test_keyutils.py:247", "tests/unit/other.py:247"),
        valid_log.replace(f"{source_path}:501", "qutebrowser/other.py:501"),
        valid_log.replace("AssertionError", "ConnectionError"),
        valid_log.replace(
            "ERROR collecting tests/unit/keyinput/test_keyutils.py",
            "ERROR collecting tests/unit/other.py",
        ),
    )
    for log in rejected_logs:
        assert namespace["_plan_log_failure_proof_matches"](
            proof, log, proof_text, command, command
        ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command + " changed"
    ) is False


def test_prolite_pytest_collection_candidate_exception_covers_every_target_file(tmp_path):
    namespace = _remote_namespace(tmp_path)
    targets = [
        "tests/unit/keyinput/test_bindingtrie.py::test_target[param]",
        "tests/unit/keyinput/test_keyutils.py::test_other[param]",
    ]
    source_path = "qutebrowser/keyinput/keyutils.py"
    proof = {
        "kind": "pytest_structured_reports",
        "targets": targets,
        "candidate_source_paths": [source_path],
    }
    proof_text = _pytest_proof_text([], exitstatus=4)
    command = "exact multi-file pytest command"
    valid_log = (
        "ERROR collecting tests/unit/keyinput/test_bindingtrie.py\n"
        "tests/unit/keyinput/test_bindingtrie.py:32: in <module>\n"
        "ERROR collecting tests/unit/keyinput/test_keyutils.py\n"
        "tests/unit/keyinput/test_keyutils.py:502: in TestKeySequence\n"
        f"{source_path}:501: in _convert_key\n"
        "E   AssertionError: <Ctrl+Alt+y>\n"
    )

    assert namespace["_plan_log_failure_proof_matches"](
        proof, valid_log, proof_text, command, command
    ) is True
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log.replace(
            "ERROR collecting tests/unit/keyinput/test_bindingtrie.py\n",
            "",
        ),
        proof_text,
        command,
        command,
    ) is False
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        valid_log + "ERROR collecting tests/unit/keyinput/test_unrelated.py\n",
        proof_text,
        command,
        command,
    ) is False


def test_prolite_pytest_parameter_parent_fallback_proves_all_collected_instances(tmp_path):
    namespace = _remote_namespace(tmp_path)
    parent = "tests/test_many.py::test_case"
    targets = [parent + "[dataset-a]", parent + "[dataset-b]"]
    proof = {
        "kind": "pytest_structured_reports",
        "targets": targets,
        "parameter_fallback_parents": [parent],
    }
    actual_nodes = [parent + "[runtime-repr-1]", parent + "[runtime-repr-2]"]
    complete = _pytest_proof_text(actual_nodes)

    assert namespace["_plan_log_proof_matches"](proof, "2 passed", complete) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "1 passed",
        _pytest_proof_text(actual_nodes[:1]),
    ) is True
    assert namespace["_plan_log_proof_matches"](
        proof,
        "2 passed",
        _pytest_proof_text([*actual_nodes, "tests/other.py::test_case[runtime]"]),
    ) is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        "1 failed",
        _pytest_proof_text(actual_nodes, call_outcome="failed"),
    ) is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        "no tests ran in 0.01s",
        _pytest_proof_text([], exitstatus=5),
    ) is False


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
