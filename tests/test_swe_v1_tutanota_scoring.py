"""Tutanota runner and structured target-proof regressions."""

from __future__ import annotations

import json

from swe_v1_prolite_runner_test_support import _command_namespace, _proof_namespace

from opencollab_eval.engine.swe_v1_remote_target_proof import _patch_tutanota_suite


def test_tutanota_uses_real_test_runner_and_proves_completed_suites():
    command_namespace = _command_namespace()
    expected = [
        "test/tests/api/worker/rest/EntityRestClientTest.js | test suite",
        "test/tests/api/worker/rest/ServiceExecutorTest.js | test suite",
    ]
    command = command_namespace["prolite_test_command"](
        {
            "repo": "tutao/tutanota",
            "repo_language": "ts",
            "selected_test_files_to_run": [],
        },
        expected,
    )

    assert "OPENCOLLAB_OSPEC_RESULTS" in command
    assert command.startswith("python3 -I -c ")
    assert "EntityRestClient" in command
    assert "ServiceExecutor" in command
    assert "opencollabResults" in command
    assert 'Path("test").rglob("*")' in command
    assert "npm_config_nodedir=/usr/local npm run test:app" in command
    assert "npm_config_nodedir=/usr/local npm run testapi -- -c" in command
    assert "npm_config_nodedir=/usr/local npm run testclient" in command

    patched = _patch_tutanota_suite(
        "const errCount = o.report(results, stats)\n",
        expected,
    )
    assert patched.index("const errCount = o.report(results, stats)") < patched.index(
        "OPENCOLLAB_OSPEC_RESULTS"
    )
    split_reporter = _patch_tutanota_suite(
        "export function reportTest(results: any, stats: any) {\n"
        "  const errCount = o.report(results, stats)\n"
        "  if (errCount !== 0) process.exit(1)\n"
        "}\n",
        expected,
    )
    assert split_reporter.index("OPENCOLLAB_OSPEC_RESULTS") < split_reporter.index(
        "process.exit(1)"
    )
    assert command_namespace["prolite_test_command"](
        {
            "repo": "tutao/tutanota",
            "repo_language": "ts",
            "selected_test_files_to_run": [],
        },
        [],
    ) == ""

    proof_namespace = _proof_namespace()
    proof = proof_namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "tutao/tutanota"},
        expected,
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            [
                {
                    "task": "loads",
                    "context": ["EntityRestClient", "Load"],
                    "pass": True,
                },
                {
                    "task": "posts",
                    "context": ["ServiceExecutor", "POST"],
                    "pass": True,
                },
            ]
        ),
    )
    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []

    partial = proof_namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "tutao/tutanota"},
        expected,
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            {
                "targets": expected,
                "results": [
                    {
                        "task": "loads",
                        "context": ["EntityRestClient", "Load"],
                        "pass": True,
                    }
                ],
            }
        ),
    )
    assert partial["ok"] is False
    assert partial["passed"] == [expected[0]]
    assert partial["missing"] == [expected[1]]


def test_tutanota_proof_requires_exact_suite_identity_across_split_markers():
    proof_namespace = _proof_namespace()
    row = {"repo_language": "ts", "repo": "tutao/tutanota"}
    expected = [
        "test/tests/FooTest.js | test suite",
        "test/tests/FooExtraTest.js | test suite",
    ]
    prefix_collision = proof_namespace["fail_to_pass_execution_proof"](
        row,
        expected,
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            {
                "targets": expected,
                "results": [
                    {"task": "helper", "context": ["FooExtra"], "pass": True}
                ],
            }
        ),
    )
    assert prefix_collision["ok"] is False
    assert prefix_collision["observed"] == [expected[1]]
    assert prefix_collision["missing"] == [expected[0]]

    source_bound = proof_namespace["fail_to_pass_execution_proof"](
        row,
        [expected[0]],
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            {
                "targets": [expected[0]],
                "results": [
                    {
                        "task": "test case",
                        "context": "renamed suite > test case",
                        "source": "api/FooTest.ts",
                        "pass": True,
                    }
                ],
            }
        ),
    )
    assert source_bound["ok"] is True
    assert source_bound["passed"] == [expected[0]]

    split_markers = "\n".join(
        [
            "OPENCOLLAB_OSPEC_RESULTS "
            + json.dumps(
                {
                    "targets": expected,
                    "results": [
                        {"task": "api case", "context": ["Foo"], "pass": True}
                    ],
                }
            ),
            "OPENCOLLAB_OSPEC_RESULTS "
            + json.dumps(
                {
                    "targets": expected,
                    "results": [
                        {
                            "task": "client case",
                            "context": ["FooExtra"],
                            "pass": True,
                        }
                    ],
                }
            ),
        ]
    )
    complete = proof_namespace["fail_to_pass_execution_proof"](
        row,
        expected,
        0,
        split_markers,
    )
    assert complete["ok"] is True
    assert complete["passed"] == expected
    assert complete["missing"] == []
