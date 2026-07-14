"""Go adapter execution-proof tests."""

from __future__ import annotations

import json

from swe_v1_prolite_runner_test_support import _remote_namespace

from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind


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


def test_dynamic_go_plan_rejects_a_command_suffix_after_the_trusted_program(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "go", "repo": "gravitational/teleport"},
        ["TestColumn", "TestMessage"],
    )

    assert validated_test_plan_kind(plan, require_commands=True) == (
        "go-test-json-discovery"
    )

    plan["commands"][0] += "; true"

    assert validated_test_plan_kind(plan, require_commands=True) is None


def test_dynamic_go_plan_supports_colons_in_subtest_names(tmp_path):
    namespace = _remote_namespace(tmp_path)
    targets = [
        "Test_redhatBase_parseInstalledPackagesLine/old:_package_1",
        "Test_redhatBase_parseInstalledPackagesLine/new:_package_2",
    ]

    plan = namespace["prolite_test_plan"]({"repo_language": "go"}, targets)

    assert plan["adapter"] == "go-test-json-discovery"
    assert plan["coverage_verified"] is True
    assert plan["declared_targets"] == targets
    assert validated_test_plan_kind(plan, require_commands=True) == (
        "go-test-json-discovery"
    )


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
