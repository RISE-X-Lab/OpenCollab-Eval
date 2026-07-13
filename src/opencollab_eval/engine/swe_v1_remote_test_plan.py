"""Cross-language ProLite test-plan construction and proof dispatch."""

# ruff: noqa: E501, F403, F405, I001

from opencollab_eval.engine.swe_test_plan_contract import (
    NOOP_TEST_COMMANDS as _NOOP_TEST_COMMANDS,
    is_runnable_test_command as _is_runnable_test_command,
    validated_test_plan_kind,
)
from opencollab_eval.engine.swe_v1_go_failure_proof import *
from opencollab_eval.engine.swe_v1_remote_go_targets import *
from opencollab_eval.engine.swe_v1_remote_javascript_proof import *
from opencollab_eval.engine.swe_v1_remote_pytest_proof import *
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *
from opencollab_eval.engine.swe_v1_remote_target_proof import *


def _plan_log_proof_matches(proof, log_text, proof_text=""):
    """Require positive per-target evidence from a completed test command."""
    if not proof:
        return True
    if proof.get("kind") == "pytest_structured_reports":
        targets = proof.get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        if any(not isinstance(target, str) or not target for target in targets):
            return False
        return _pytest_structured_proof_matches(
            targets,
            proof_text,
            log_text,
            proof.get("parameter_fallback_parents"),
        )
    if proof.get("kind") == "js_parser_backed_targets":
        targets = proof.get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        result = fail_to_pass_execution_proof(
            {
                "repo_language": proof.get("repo_language") or "",
                "repo": proof.get("repo") or "",
            },
            targets,
            0,
            log_text,
        )
        return result.get("ok") is True
    if proof.get("kind") != "go_json_test_pass":
        return False
    return go_pass_proof_matches(proof, log_text)



def _plan_log_failure_proof_matches(
    proof,
    log_text,
    proof_text="",
    expected_command="",
    observed_command="",
):
    """Require one exact declared target to be observed with a failed result."""
    if not isinstance(proof, dict):
        return False
    if proof.get("kind") == "pytest_structured_reports":
        targets = proof.get("targets")
        return bool(
            isinstance(targets, list)
            and targets
            and all(isinstance(target, str) and target for target in targets)
            and (
                _pytest_structured_failure_proof_matches(
                    targets,
                    proof_text,
                    proof.get("parameter_fallback_parents"),
                )
                or _pytest_collection_failure_proof_matches(
                    targets,
                    proof_text,
                    log_text,
                    expected_command,
                    observed_command,
                    proof.get("candidate_source_paths"),
                    proof.get("target_imports"),
                    proof.get("repo") or "",
                )
            )
        )
    if proof.get("kind") == "js_parser_backed_targets":
        targets = proof.get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        parsed = fail_to_pass_execution_proof(
            {
                "repo_language": proof.get("repo_language") or "",
                "repo": proof.get("repo") or "",
            },
            targets,
            1,
            log_text,
        )
        failed = parsed.get("failed")
        return bool(
            isinstance(failed, list)
            and any(target in failed for target in targets)
        ) or _js_suite_load_failure_proof_matches(
            proof,
            log_text,
            expected_command,
            observed_command,
        )
    if proof.get("kind") != "go_json_test_pass":
        return False
    return go_failure_proof_matches(
        proof,
        log_text,
        expected_command=expected_command,
        observed_command=observed_command,
    )



def _test_plan(
    adapter,
    declared_targets,
    target_batches,
    commands,
    coverage,
    proofs=None,
):
    declared_targets = [str(item) for item in declared_targets if str(item)]
    commands = [str(item) for item in commands if _is_runnable_test_command(str(item))]
    target_batches = [[str(item) for item in batch] for batch in target_batches]
    flattened_targets = [item for batch in target_batches for item in batch]
    proof_batches = list(proofs or [])
    return {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": adapter,
        "coverage": coverage,
        "coverage_verified": bool(
            declared_targets
            and commands
            and len(commands) == len(target_batches)
            and flattened_targets == declared_targets
            and (not proof_batches or len(proof_batches) == len(target_batches))
        ),
        "declared_targets": declared_targets,
        "target_batches": target_batches,
        "commands": commands,
        "proofs": proof_batches,
    }


def _unsupported_test_plan(tests):
    return _test_plan("unsupported", tests, [], [], "none")


def _targets_with_paths(tests):
    mapped = []
    for raw in tests:
        declared = str(raw or "")
        path = declared.split(" | ", 1)[0].strip()
        if not path or not ("/" in path or "." in path):
            return []
        mapped.append((declared, path))
    return mapped


def prolite_test_plan(
    row,
    tests,
    max_args=80,
    max_chars=24000,
    target_file="",
    candidate_source_paths=None,
):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if not tests:
        return _unsupported_test_plan(tests)
    python_targets = language == "python" or (
        not language and any("::" in item or item.endswith(".py") for item in tests)
    )
    if python_targets:
        # Candidate code and Pytest reporting hooks share one interpreter.
        # An external result boundary is required before Python targets can
        # produce executable pass evidence.
        return _unsupported_test_plan(tests)
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        specs = [go_exact_test_spec(item) for item in tests]
        if any(spec is None for spec in specs):
            if all(
                re.fullmatch(r"Test[A-Za-z0-9_]*(?:/[A-Za-z0-9_.-]+)*", item)
                for item in tests
            ):
                return _test_plan(
                    "go-test-json-discovery",
                    tests,
                    [tests],
                    [go_test_command(tests)],
                    "runtime_discovered_exact_test_events",
                    proofs=[
                        {
                            "kind": "go_json_test_pass",
                            "tests": tests,
                            "dynamic_discovery": True,
                        }
                    ],
                )
            return _unsupported_test_plan(tests)
        exact_specs = [spec for spec in specs if spec is not None]
        target_batches = [[spec["declared_target"]] for spec in exact_specs]
        commands = [
            "go test -count=1 -json "
            + shlex.quote(spec["package"])
            + " -run "
            + shlex.quote(spec["run_pattern"])
            for spec in exact_specs
        ]
        proofs = [
            {
                "kind": "go_json_test_pass",
                "test": spec["test"],
                "package": spec["package"],
                "test_file": spec["test_file"],
            }
            for spec in exact_specs
        ]
        return _test_plan(
            "go-test-json",
            tests,
            target_batches,
            commands,
            "exact_test_events",
            proofs=proofs,
        )
    if language in {"js", "javascript", "typescript", "ts"} or repo in {
        "nodebb/nodebb",
        "protonmail/webclients",
        "element-hq/element-web",
        "tutao/tutanota",
    }:
        files = canonical_js_test_files(tests, selected)
        if not files:
            return _unsupported_test_plan(tests)
        if repo == "nodebb/nodebb":
            command = mocha_test_command(tests, selected, target_file)
            adapter = "mocha-json-stream"
        elif repo == "tutao/tutanota":
            command = tutanota_test_command(tests)
            adapter = "ospec-structured-results"
        else:
            command = jest_test_command(files)
            adapter = "jest-json-verbose"
        proof = {
            "kind": "js_parser_backed_targets",
            "targets": tests,
            "test_files": files,
            "repo_language": language,
            "repo": repo,
        }
        if adapter == "mocha-json-stream" and target_file:
            proof["target_file"] = target_file
        suite_module_mocks = _js_suite_module_mock_bindings(row, files)
        if suite_module_mocks:
            proof["suite_module_mocks"] = suite_module_mocks
        return _test_plan(
            adapter,
            tests,
            [tests],
            [command],
            "parser_backed_exact_targets",
            proofs=[proof],
        )
    # Dataset-provided shell snippets have no machine-checkable relationship to
    # declared targets. A successful arbitrary command therefore cannot prove
    # FAIL_TO_PASS execution.
    return _unsupported_test_plan(tests)


def prolite_test_command(row, tests, target_file=""):
    plan = prolite_test_plan(row, tests, target_file=target_file)
    return " && ".join(plan["commands"])


def prolite_test_plan_script(plan, evidence_prefix, proof_nonce="proof"):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(evidence_prefix)):
        raise ValueError("invalid test evidence prefix")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(proof_nonce)):
        raise ValueError("invalid pytest proof nonce")
    plan_kind = validated_test_plan_kind(
        plan,
        require_commands=bool(plan.get("commands")) if isinstance(plan, dict) else True,
    )
    if plan_kind is None:
        return "#!/usr/bin/env bash\necho 'untrusted test plan is unsupported' >&2\nexit 86\n"
    lines = ["#!/usr/bin/env bash", "set +e", "overall_status=0"]
    for index, command in enumerate(plan.get("commands") or [], 1):
        stem = f"/eval_output/{evidence_prefix}.batch_{index:03d}"
        lines.extend(
            [
                f"printf '%s\\n' {shlex.quote(command)} > {stem}.command",
                f"bash -c {shlex.quote(command)} > {stem}.log 2>&1",
                "batch_status=$?",
                f"printf '%s\\n' \"$batch_status\" > {stem}.exit",
                f"cat {stem}.log",
                'if [ "$overall_status" -eq 0 ] && [ "$batch_status" -ne 0 ]; then',
                "  overall_status=$batch_status",
                "fi",
            ]
        )
    lines.extend(['exit "$overall_status"', ""])
    return "\n".join(lines)




__all__ = [
    "_NOOP_TEST_COMMANDS",
    "_is_runnable_test_command",
    "_plan_log_failure_proof_matches",
    "_plan_log_proof_matches",
    "_targets_with_paths",
    "_test_plan",
    "_unsupported_test_plan",
    "prolite_test_command",
    "prolite_test_plan",
    "prolite_test_plan_script",
]
