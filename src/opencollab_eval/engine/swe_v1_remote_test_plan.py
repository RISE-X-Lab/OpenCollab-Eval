"""Cross-language ProLite test-plan construction and proof dispatch."""

# ruff: noqa: E501, F403, F405, I001

import math
import shlex

from opencollab_eval.engine.swe_test_plan_contract import (
    NOOP_TEST_COMMANDS as _NOOP_TEST_COMMANDS,
    dynamic_go_targets_supported,
    is_go_test_name,
    is_runnable_test_command as _is_runnable_test_command,
    javascript_runtime_dependencies,
    validated_test_plan_kind,
)
from opencollab_eval.engine.swe_v1_go_failure_proof import *
from opencollab_eval.engine.swe_v1_remote_go_targets import *
from opencollab_eval.engine.swe_v1_remote_javascript_proof import *
from opencollab_eval.engine.swe_v1_remote_pytest_proof import *
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *
from opencollab_eval.engine.swe_v1_remote_target_proof import *


# Keep this helper self-contained: it is embedded in the evaluation container
# and therefore cannot import the OpenCollab package from the host workspace.
# ``start_new_session`` gives the command its own process group so a timeout
# cannot leave a compiler/test descendant running after the shell exits.
_BOUNDED_COMMAND_RUNNER_SOURCE = r'''import os
import signal
import subprocess
import sys
import time


def _process_group_exists(process):
    """Return whether the owned process group still has a member."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        try:
            return process.poll() is None
        except (AttributeError, OSError):
            return False
    try:
        killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # A permission/error response means that absence was not proven.
        return True
    return True


def _kill_and_reap(process):
    """Kill a command group and catch descendants forked during cleanup."""
    killpg = getattr(os, "killpg", None)
    deadline = time.monotonic() + 5.0
    empty_scans = 0
    while time.monotonic() < deadline:
        try:
            if killpg is not None:
                killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=min(0.1, remaining))
        except (OSError, ChildProcessError, subprocess.TimeoutExpired):
            pass
        try:
            leader_alive = process.poll() is None
        except (AttributeError, OSError):
            leader_alive = False
        group_alive = _process_group_exists(process)
        if not leader_alive and not group_alive:
            empty_scans += 1
            if empty_scans >= 2:
                return True
        else:
            empty_scans = 0
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    try:
        process.wait(timeout=0.1)
    except (OSError, ChildProcessError, subprocess.TimeoutExpired):
        pass
    return not _process_group_exists(process) and process.poll() is not None


try:
    timeout = float(sys.argv[1])
    command = bytes.fromhex(sys.argv[2]).decode("utf-8")
except (IndexError, TypeError, ValueError, OverflowError, UnicodeDecodeError):
    raise SystemExit(124)
if timeout <= 0 or timeout != timeout or timeout == float("inf") or timeout == float("-inf"):
    raise SystemExit(124)
try:
    process = subprocess.Popen(["bash", "-c", command], start_new_session=True)
except OSError:
    raise SystemExit(127)
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    if _kill_and_reap(process):
        raise SystemExit(124)
    raise SystemExit(125)
except BaseException:
    _kill_and_reap(process)
    raise
if _process_group_exists(process):
    # A command that exits successfully while leaving a same-session
    # descendant behind is not a clean test execution.  Reap the owned group
    # within the same bounded cleanup window and keep the result technical so
    # the evaluator cannot record a false pass.
    if not _kill_and_reap(process):
        raise SystemExit(125)
    raise SystemExit(125)
if returncode < 0:
    returncode = min(255, 128 - returncode)
raise SystemExit(min(255, returncode))
'''


def _bounded_command_execution(command: str, timeout_argument: str) -> str:
    """Build a dependency-free bounded command invocation for the container."""

    return (
        "python3 -c "
        + shlex.quote(_BOUNDED_COMMAND_RUNNER_SOURCE)
        # The first argument after ``-c`` becomes ``sys.argv[1]``; adding a
        # conventional ``--`` sentinel here would shift both wrapper inputs
        # and make every invocation fail before launching the child.
        + " "
        + timeout_argument
        + " "
        # Encode the shell command as hex before crossing the nested
        # ``bash -c`` boundary.  Quoting the raw command twice would strip
        # quotes from commands such as ``python3 -c '...'``.
        + shlex.quote(command.encode("utf-8").hex())
    )


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
            proof.get("command_sha256"),
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
                    proof.get("command_sha256"),
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
                    proof.get("command_sha256"),
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
        ) or _js_candidate_failure_proof_matches(
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


def _plan_log_skip_proof_matches(proof, proof_text=""):
    """Recognize a complete command whose declared Pytest target was skipped."""
    if not isinstance(proof, dict) or proof.get("kind") != "pytest_structured_reports":
        return False
    targets = proof.get("targets")
    return bool(
        isinstance(targets, list)
        and targets
        and all(isinstance(target, str) and target for target in targets)
        and _pytest_structured_skip_proof_matches(
            targets,
            proof_text,
            proof.get("parameter_fallback_parents"),
            proof.get("command_sha256"),
        )
    )



def _test_plan(
    adapter,
    declared_targets,
    target_batches,
    commands,
    coverage,
    proofs=None,
    runtime_dependencies=None,
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
        "runtime_dependencies": list(runtime_dependencies or []),
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
    candidate_added_go_modules=None,
):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if not tests:
        return _unsupported_test_plan(tests)
    go_target_syntax = any(
        str(item).split(" | ", 1)[0].split("::", 1)[0].strip().endswith(".go")
        for item in tests
    )
    python_targets = language == "python" or (
        not language
        and not go_target_syntax
        and any("::" in item or item.endswith(".py") for item in tests)
    )
    if python_targets:
        python_candidate_paths = [
            str(path)
            for path in candidate_source_paths or []
            if str(path).endswith(".py")
        ]
        tests = compact_python_test_targets(
            tests,
            selected,
            max_args=max_args,
            max_chars=max_chars,
        )
        target_batches, execution_batches, fallback_batches = python_parameter_fallback_batches(
            tests,
            max_args=max_args,
            max_chars=max_chars,
        )
        pytest_prefix = "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        if repo == "qutebrowser/qutebrowser":
            pytest_prefix = (
                "xvfb-run -a python -m pytest --no-xvfb "
                "-p opencollab_pytest_proof -q -rA -o addopts= "
            )
        commands = [
            pytest_prefix + " ".join(shlex.quote(item) for item in batch)
            for batch in execution_batches
        ]
        proofs = []
        for batch, fallback_parents, command in zip(
            target_batches, fallback_batches, commands, strict=True
        ):
            proof = {
                "kind": "pytest_structured_reports",
                "targets": list(batch),
                "command_sha256": hashlib.sha256(
                    "\0".join(shlex.split(command)).encode("utf-8")
                ).hexdigest(),
            }
            if fallback_parents:
                proof["parameter_fallback_parents"] = fallback_parents
            if python_candidate_paths:
                proof["candidate_source_paths"] = python_candidate_paths
            target_imports = _python_test_patch_import_bindings(row, batch)
            if target_imports:
                proof["repo"] = repo
                proof["target_imports"] = target_imports
            proofs.append(proof)
        return _test_plan(
            "pytest",
            tests,
            target_batches,
            commands,
            "parameter_parent_targets" if any(fallback_batches) else "exact_targets",
            proofs=proofs,
        )
    if (
        language == "go"
        or go_target_syntax
        or repo.endswith("/vuls")
        or repo.endswith("/teleport")
        or repo.endswith("/navidrome")
    ):
        specs = [go_exact_test_spec(item) for item in tests]
        if any(spec is None for spec in specs):
            if all(is_go_test_name(item) for item in tests) and dynamic_go_targets_supported(tests):
                proof = {
                    "kind": "go_json_test_pass",
                    "tests": tests,
                    "dynamic_discovery": True,
                }
                if candidate_source_paths:
                    proof["candidate_source_paths"] = list(candidate_source_paths)
                if candidate_added_go_modules:
                    proof["candidate_added_go_modules"] = list(candidate_added_go_modules)
                return _test_plan(
                    "go-test-json-discovery",
                    tests,
                    [tests],
                    [go_test_command(tests)],
                    "runtime_discovered_exact_test_events",
                    proofs=[proof],
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
        proofs = []
        for spec in exact_specs:
            proof = {
                "kind": "go_json_test_pass",
                "test": spec["test"],
                "package": spec["package"],
                "test_file": spec["test_file"],
            }
            if candidate_source_paths:
                proof["candidate_source_paths"] = list(candidate_source_paths)
            if candidate_added_go_modules:
                proof["candidate_added_go_modules"] = list(candidate_added_go_modules)
            proofs.append(proof)
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
        test_patch_files = _js_test_patch_files(row)
        files = verified_js_test_files(tests, selected, test_patch_files)
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
        if candidate_source_paths:
            proof["candidate_source_paths"] = list(candidate_source_paths)
        if files != declared_js_test_files(tests):
            proof["selected_test_files"] = selected
            proof["test_patch_files"] = test_patch_files
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
            runtime_dependencies=javascript_runtime_dependencies(adapter),
        )
    # Dataset-provided shell snippets have no machine-checkable relationship to
    # declared targets. A successful arbitrary command therefore cannot prove
    # FAIL_TO_PASS execution.
    return _unsupported_test_plan(tests)


def prolite_test_command(row, tests, target_file=""):
    plan = prolite_test_plan(row, tests, target_file=target_file)
    return " && ".join(plan["commands"])


def prolite_test_plan_script(
    plan,
    evidence_prefix,
    proof_nonce="proof",
    *,
    controller_timeout=None,
    shared_deadline_env=None,
):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(evidence_prefix)):
        raise ValueError("invalid test evidence prefix")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(proof_nonce)):
        raise ValueError("invalid pytest proof nonce")
    if shared_deadline_env is not None and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", str(shared_deadline_env)
    ):
        raise ValueError("invalid shared deadline environment variable")
    timeout_value = None
    if controller_timeout is not None:
        if isinstance(controller_timeout, bool):
            raise ValueError("controller timeout must be finite and positive")
        try:
            timeout_value = float(controller_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("controller timeout must be finite and positive") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("controller timeout must be finite and positive")
    plan_kind = validated_test_plan_kind(
        plan,
        require_commands=bool(plan.get("commands")) if isinstance(plan, dict) else True,
    )
    if plan_kind is None:
        return "#!/usr/bin/env bash\necho 'untrusted test plan is unsupported' >&2\nexit 86\n"
    commands = plan.get("commands") or []
    # A direct evaluation invokes the fail-to-pass and pass-to-pass scripts as
    # separate processes.  ``shared_deadline_env`` lets that caller provide
    # one absolute monotonic deadline while retaining the historical behavior
    # for standalone callers that omit it.
    shared_deadline = controller_timeout is not None and (
        len(commands) > 1 or shared_deadline_env is not None
    )
    lines = ["#!/usr/bin/env bash", "set +e", "overall_status=0"]
    stop_after_cleanup_failure = shared_deadline and len(commands) > 1
    if stop_after_cleanup_failure:
        lines.append("stop_after_cleanup_failure=0")
    if shared_deadline:
        if shared_deadline_env is None:
            lines.extend(
                [
                    "controller_deadline=$(python3 -c 'import time,sys; print(time.monotonic()+float(sys.argv[1]))' "
                    + shlex.quote(str(timeout_value))
                    + ")",
                    "if [ -z \"${controller_deadline:-}\" ]; then overall_status=124; fi",
                ]
            )
        else:
            env_ref = f"${{{shared_deadline_env}:-}}"
            lines.extend(
                [
                    f'if [ -n "{env_ref}" ]; then',
                    f'  controller_deadline="${shared_deadline_env}"',
                    "else",
                    "  controller_deadline=$(python3 -c 'import time,sys; print(time.monotonic()+float(sys.argv[1]))' "
                    + shlex.quote(str(timeout_value))
                    + ")",
                    "fi",
                    "if [ -z \"${controller_deadline:-}\" ]; then overall_status=124; fi",
                ]
            )
    for index, command in enumerate(commands, 1):
        stem = f"/eval_output/{evidence_prefix}.batch_{index:03d}"
        proofs = plan.get("proofs") or []
        proof = proofs[index - 1] if index <= len(proofs) else None
        execution_command = command
        is_pytest_controller = (
            isinstance(proof, dict)
            and proof.get("kind") == "pytest_structured_reports"
        )
        if is_pytest_controller:
            proof_path = f"{stem}.proof.{proof_nonce}.jsonl"
            candidate_path_args = [
                value
                for path in proof.get("candidate_source_paths") or []
                for value in ("--candidate-source-path", str(path))
            ]
            controller_args = [
                    "python3",
                    "/eval_input/opencollab_pytest_controller.py",
                    "--proof-output",
                    proof_path,
                    "--command-sha256",
                    str(proof.get("command_sha256") or ""),
            ]
            if controller_timeout is not None and not shared_deadline:
                controller_args.extend(
                    ["--event-timeout-seconds", str(controller_timeout)]
                )
            elif shared_deadline:
                controller_args.extend(
                    ["--event-timeout-seconds", "__OPENCOLLAB_BATCH_TIMEOUT__"]
                )
            controller_args.extend(
                [
                    *candidate_path_args,
                    "--",
                    *shlex.split(command),
                ]
            )
            execution_command = shlex.join(controller_args)
            if shared_deadline:
                execution_command = execution_command.replace(
                    "__OPENCOLLAB_BATCH_TIMEOUT__", '"$batch_timeout"', 1
                )
        elif timeout_value is not None:
            execution_command = _bounded_command_execution(
                command,
                '"$batch_timeout"'
                if shared_deadline
                else shlex.quote(str(timeout_value)),
            )
        # The privileged pytest controller has its own event-stream deadline,
        # but startup (importing pytest, walking the repository, or dropping
        # privileges) happens before that loop begins.  Keep the same outer
        # process-group watchdog around it so a wedged image cannot bypass the
        # generated plan's total budget.
        if timeout_value is not None and is_pytest_controller:
            execution_command = _bounded_command_execution(
                execution_command,
                '"$batch_timeout"'
                if shared_deadline
                else shlex.quote(str(timeout_value)),
            )
        batch_prefix = []
        if stop_after_cleanup_failure:
            batch_prefix.extend(
                [
                    'if [ "${stop_after_cleanup_failure:-0}" -eq 1 ]; then',
                    "  batch_status=125",
                    f"  printf '%s\\n' \"$batch_status\" > {stem}.exit",
                    "else",
                ]
            )
        if shared_deadline:
            batch_prefix.extend(
                [
                'batch_timeout=$(python3 -c \'import math,sys,time; d=float(sys.argv[1]); l=float(sys.argv[2]); r=d-time.monotonic(); sys.exit(124) if (not math.isfinite(d) or not math.isfinite(l) or not math.isfinite(r) or r <= 0) else print(min(l,r))\' "$controller_deadline" '
                + shlex.quote(str(timeout_value))
                + ")",
                "batch_timeout_status=$?",
                "export batch_timeout",
                'if [ "$batch_timeout_status" -ne 0 ] || [ -z "${batch_timeout:-}" ]; then',
                "  batch_status=124",
                f"  printf '%s\\n' \"$batch_status\" > {stem}.exit",
                "  if [ \"$overall_status\" -eq 0 ]; then overall_status=$batch_status; fi",
                "else",
                ]
            )
        lines.extend(
            [
                f"printf '%s\\n' {shlex.quote(command)} > {stem}.command",
                *batch_prefix,
                f"bash -c {shlex.quote(execution_command)} > {stem}.log 2>&1",
                "batch_status=$?",
                f"printf '%s\\n' \"$batch_status\" > {stem}.exit",
                f"cat {stem}.log",
                'if [ "$overall_status" -eq 0 ] && [ "$batch_status" -ne 0 ]; then',
                "  overall_status=$batch_status",
                "fi",
            ]
        )
        if shared_deadline:
            lines.append("fi")
        if stop_after_cleanup_failure:
            lines.extend(
                [
                    'if [ "${batch_status:-0}" -eq 125 ]; then',
                    "  stop_after_cleanup_failure=1",
                    "fi",
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
    "_plan_log_skip_proof_matches",
    "_targets_with_paths",
    "_test_plan",
    "_unsupported_test_plan",
    "prolite_test_command",
    "prolite_test_plan",
    "prolite_test_plan_script",
]
