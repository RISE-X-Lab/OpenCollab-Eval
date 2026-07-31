"""JavaScript adapter execution-proof tests."""

from __future__ import annotations

import json

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest

from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind
from opencollab_eval.engine.swe_v1_remote_target_proof import (
    jest_test_command,
    mocha_test_command,
    tutanota_test_command,
)


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
            "test_files": ["test/widget.test.js"],
            "repo_language": language,
            "repo": "",
        }
    ]


@pytest.mark.parametrize(
    ("row", "declared_target", "wrong_command"),
    [
        (
            {"repo_language": "javascript"},
            "test/a.test.js",
            jest_test_command(["test/b.test.js"]),
        ),
        (
            {"repo_language": "javascript", "repo": "nodebb/nodebb"},
            "test/a.js | suite A",
            mocha_test_command(["test/b.js | suite B"], ["test/b.js"]),
        ),
        (
            {"repo_language": "typescript", "repo": "tutao/tutanota"},
            "test/tests/FooTest.ts | Foo",
            tutanota_test_command(["test/tests/BarTest.ts | Bar"]),
        ),
    ],
)
def test_javascript_plan_rejects_commands_for_a_different_declared_target(
    tmp_path,
    row,
    declared_target,
    wrong_command,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](row, [declared_target])

    assert validated_test_plan_kind(plan, require_commands=True) == plan["adapter"]

    plan["commands"] = [wrong_command]

    assert validated_test_plan_kind(plan, require_commands=True) is None


def test_mocha_target_file_command_is_bound_to_the_declared_titles(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target_file = "/eval_input/f2p.targets.json"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "javascript", "repo": "nodebb/nodebb"},
        ["test/a.js | suite A"],
        target_file=target_file,
    )

    assert validated_test_plan_kind(plan, require_commands=True) == (
        "mocha-json-stream"
    )

    plan["commands"] = [
        mocha_test_command(
            ["test/b.js | suite B"],
            ["test/b.js"],
            target_file,
        )
    ]

    assert validated_test_plan_kind(plan, require_commands=True) is None


@pytest.mark.parametrize(
    ("row", "declared_target", "aliased_file", "aliased_command"),
    [
        (
            {"repo_language": "typescript"},
            "src/app/utils/replaceLocalURL.test.ts | should replace",
            "applications/evil/src/app/utils/replaceLocalURL.test.ts",
            jest_test_command(
                ["applications/evil/src/app/utils/replaceLocalURL.test.ts"]
            ),
        ),
        (
            {"repo_language": "javascript", "repo": "nodebb/nodebb"},
            "test/topics.js | title",
            "packages/evil/test/topics.js",
            mocha_test_command(
                ["test/topics.js | title"],
                ["packages/evil/test/topics.js"],
            ),
        ),
    ],
)
def test_javascript_plan_rejects_a_self_reported_suffix_alias(
    tmp_path,
    row,
    declared_target,
    aliased_file,
    aliased_command,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](row, [declared_target])

    plan["proofs"][0]["test_files"] = [aliased_file]
    plan["commands"] = [aliased_command]

    assert validated_test_plan_kind(plan, require_commands=True) is None


def test_javascript_planner_fails_closed_for_an_untrusted_workspace_alias(tmp_path):
    namespace = _remote_namespace(tmp_path)
    declared = "src/app/utils/replaceLocalURL.test.ts | should replace"
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "typescript",
            "selected_test_files_to_run": [
                "applications/drive/src/app/utils/replaceLocalURL.test.ts"
            ],
        },
        [declared],
    )

    assert plan == {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "unsupported",
        "coverage": "none",
        "coverage_verified": False,
        "declared_targets": [declared],
        "target_batches": [],
        "commands": [],
        "proofs": [],
        "runtime_dependencies": [],
    }


def test_javascript_planner_accepts_one_dataset_bound_workspace_alias(tmp_path):
    namespace = _remote_namespace(tmp_path)
    declared_suite = (
        "components/drawer/views/SecurityCenter/PassAliases/PassAliases.test.tsx"
    )
    resolved_suite = "packages/components/" + declared_suite
    target = declared_suite + " | PassAliases renders the aliases list"
    test_patch = (
        f"diff --git a/{resolved_suite} b/{resolved_suite}\n"
        f"--- a/{resolved_suite}\n"
        f"+++ b/{resolved_suite}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "js",
            "repo": "protonmail/webclients",
            "selected_test_files_to_run": json.dumps(
                [declared_suite.removesuffix("x"), resolved_suite]
            ),
            "test_patch": test_patch,
        },
        [target],
    )

    assert plan["adapter"] == "jest-json-verbose"
    assert plan["coverage_verified"] is True
    assert plan["proofs"][0]["test_files"] == [resolved_suite]
    assert plan["proofs"][0]["selected_test_files"] == [
        declared_suite.removesuffix("x"),
        resolved_suite,
    ]
    assert plan["proofs"][0]["test_patch_files"] == [resolved_suite]
    assert "--config packages/components/jest.config.js" in plan["commands"][0]
    assert validated_test_plan_kind(plan, require_commands=True) == (
        "jest-json-verbose"
    )


def test_javascript_planner_prefers_test_patch_bound_workspace_alias(tmp_path):
    namespace = _remote_namespace(tmp_path)
    declared_suite = "src/app/store/_links/extendedAttributes.test.ts"
    resolved_suite = "applications/drive/" + declared_suite
    target = declared_suite + " | creates the struct from the file"
    test_patch = (
        f"diff --git a/{resolved_suite} b/{resolved_suite}\n"
        f"--- a/{resolved_suite}\n"
        f"+++ b/{resolved_suite}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "typescript",
            "repo": "protonmail/webclients",
            "selected_test_files_to_run": json.dumps([declared_suite, resolved_suite]),
            "test_patch": test_patch,
        },
        [target],
    )

    assert plan["coverage_verified"] is True
    assert plan["proofs"][0]["test_files"] == [resolved_suite]
    assert "--config applications/drive/jest.config.js" in plan["commands"][0]
    assert "--runTestsByPath " + resolved_suite in plan["commands"][0]


def test_javascript_planner_rejects_ambiguous_dataset_workspace_aliases(tmp_path):
    namespace = _remote_namespace(tmp_path)
    declared = "src/widgets/Foo.test.ts | works"
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "typescript",
            "selected_test_files_to_run": [
                "applications/a/src/widgets/Foo.test.ts",
                "applications/b/src/widgets/Foo.test.ts",
            ],
            "test_patch": (
                "diff --git a/applications/a/src/widgets/Foo.test.ts "
                "b/applications/a/src/widgets/Foo.test.ts\n"
            ),
        },
        [declared],
    )

    assert plan["adapter"] == "unsupported"
    assert plan["coverage_verified"] is False


def test_javascript_planner_prefers_a_patch_bound_suffix_over_an_exact_alias(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    exact = "src/Foo.test.ts"
    alias = "packages/a/src/Foo.test.ts"
    row = {
        "repo_language": "typescript",
        "selected_test_files_to_run": [exact, alias],
        "test_patch": f"diff --git a/{alias} b/{alias}\n--- a/{alias}\n+++ b/{alias}\n",
    }

    plan = namespace["prolite_test_plan"](row, [f"{exact} | exact suite"])

    assert plan["coverage_verified"] is True
    assert plan["proofs"][0]["test_files"] == [alias]
    assert plan["commands"] == [jest_test_command([alias])]


def test_javascript_planner_prefers_an_exact_path_over_an_unbound_suffix(tmp_path):
    namespace = _remote_namespace(tmp_path)
    exact = "src/Foo.test.ts"
    alias = "packages/a/src/Foo.test.ts"
    row = {
        "repo_language": "typescript",
        "selected_test_files_to_run": [exact, alias],
    }

    plan = namespace["prolite_test_plan"](row, [f"{exact} | exact suite"])

    assert plan["coverage_verified"] is True
    assert plan["proofs"][0]["test_files"] == [exact]
    assert plan["commands"] == [jest_test_command([exact])]


@pytest.mark.parametrize(
    ("row", "target", "forged_repo"),
    [
        (
            {"repo_language": "typescript"},
            "test/FooTest.ts | works",
            "tutao/tutanota",
        ),
        (
            {"repo_language": "javascript"},
            "test/foo.test.js | works",
            "nodebb/nodebb",
        ),
        (
            {"repo_language": "javascript", "repo": "nodebb/nodebb"},
            "test/topics.js | title",
            "",
        ),
        (
            {"repo_language": "typescript", "repo": "tutao/tutanota"},
            "test/tests/FooTest.ts | works",
            "",
        ),
    ],
)
def test_javascript_plan_rejects_adapter_and_proof_repository_mismatches(
    tmp_path,
    row,
    target,
    forged_repo,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](row, [target])

    assert validated_test_plan_kind(plan, require_commands=True) == plan["adapter"]

    plan["proofs"][0]["repo"] = forged_repo

    assert validated_test_plan_kind(plan, require_commands=True) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo_language", "TypeScript"),
        ("repo", "NodeBB/NodeBB"),
        ("repo", " nodebb/nodebb"),
    ],
)
def test_javascript_plan_rejects_noncanonical_proof_dispatch_metadata(
    tmp_path,
    field,
    value,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "javascript", "repo": "nodebb/nodebb"},
        ["test/topics.js | title"],
    )

    plan["proofs"][0][field] = value

    assert validated_test_plan_kind(plan, require_commands=True) is None


@pytest.mark.parametrize(
    "candidate_paths",
    [
        [{"path": "src/widget.ts"}],
        [["src/widget.ts"]],
        ["../src/widget.ts"],
        ["/src/widget.ts"],
        ["src//widget.ts"],
        ["src/widget.ts\nforged"],
    ],
)
def test_javascript_plan_rejects_untrusted_candidate_path_shapes(
    tmp_path,
    candidate_paths,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "javascript"},
        ["test/widget.test.js"],
    )
    plan["proofs"][0]["candidate_source_paths"] = candidate_paths

    assert validated_test_plan_kind(plan, require_commands=True) is None


def test_jest_suite_load_failure_binds_declared_mock_without_repo_special_case(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    suite = "packages/widget/Widget.test.tsx"
    declared_suite = suite
    missing_module = "@example/widget/provider"
    target = declared_suite + " | Widget renders the value"
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
            "repo": "example/widgets",
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
        log.replace(missing_module, "@example/unbound", 1),
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


def test_jest_candidate_source_failure_is_bound_to_target_and_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    suite = "test/widget.test.ts"
    source = "src/widget.ts"
    target = suite + " | Widget returns its value"
    plan = namespace["prolite_test_plan"](
        {
            "repo_language": "typescript",
            "repo": "example/widgets",
            "selected_test_files_to_run": json.dumps([suite]),
        },
        [target],
        candidate_source_paths=[source],
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
                "message": f"Test suite failed to run\nReferenceError: {source}: missingName",
            }
        ],
    }
    log = f"FAIL {suite}\n" + json.dumps(result) + "\n"

    assert proof["candidate_source_paths"] == [source]
    assert namespace["_plan_log_failure_proof_matches"](
        proof,
        log,
        "",
        command,
        command,
    )
    assert not namespace["_plan_log_failure_proof_matches"](
        proof,
        log.replace(source, "src/unrelated.ts"),
        "",
        command,
        command,
    )
