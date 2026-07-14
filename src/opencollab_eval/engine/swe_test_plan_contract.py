"""Structural trust contract for executable Pro-Lite test plans."""

from __future__ import annotations

import pathlib
import re
import shlex
from typing import Any

from opencollab_eval.engine.swe_v1_remote_target_proof import (
    declared_js_test_files,
    go_test_command,
    jest_test_command,
    mocha_test_command,
    tutanota_test_command,
)

PLAN_SCHEMA = "opencollab.prolite_test_plan.v2"
EMPTY_PLAN_KIND = "empty"
NOOP_TEST_COMMANDS = {"", "true", ":", "/bin/true"}
_PLAN_KEYS = {
    "schema",
    "adapter",
    "coverage",
    "coverage_verified",
    "declared_targets",
    "target_batches",
    "commands",
    "proofs",
}
_ADAPTER_COVERAGE = {
    "go-test-json": "exact_test_events",
    "go-test-json-discovery": "runtime_discovered_exact_test_events",
    "jest-json-verbose": "parser_backed_exact_targets",
    "mocha-json-stream": "parser_backed_exact_targets",
    "ospec-structured-results": "parser_backed_exact_targets",
}
_JAVASCRIPT_LANGUAGES = {"js", "javascript", "ts", "typescript"}
_GO_TEST_NAME = re.compile(r"Test[A-Za-z0-9_]*(?:/[A-Za-z0-9_.:-]+)*")


def is_go_test_name(value: str) -> bool:
    return _GO_TEST_NAME.fullmatch(value) is not None


def is_runnable_test_command(command: str) -> bool:
    """Recognize only command families backed by an independent parser."""

    if not isinstance(command, str) or command.strip() in NOOP_TEST_COMMANDS:
        return False
    return bool(
        re.fullmatch(r"go test -count=1 -json \S+ -run \S+", command)
        or command.startswith("if [ -x ./node_modules/.bin/jest ]; then\n")
        or command.startswith("if [ -x ./node_modules/.bin/mocha ]; then\n")
        or command.startswith("python3 -I -c ")
        and "npm run test:app" in command
        or command.startswith("python3 -I -c ")
        and "missing declared Mocha titles" in command
        and "json-stream" in command
        or command.startswith("python3 -I -c ")
        and "unable to map Go tests to packages" in command
        and '"go", "test", "-count=1", "-json"' in command
    )


def _string_list(value: Any, *, allow_empty: bool) -> list[str] | None:
    if not isinstance(value, list) or (not allow_empty and not value):
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return value


def _exact_go_binding(target: str) -> tuple[str, str, str, str] | None:
    declared = target.split(" | ", 1)[0].strip()
    if declared.count("::") != 1:
        return None
    raw_path, test = (part.strip() for part in declared.split("::", 1))
    path = raw_path.replace("\\", "/").removeprefix("./")
    if not path.endswith("_test.go") or not is_go_test_name(test):
        return None
    parent = pathlib.PurePosixPath(path).parent.as_posix()
    package = "." if parent in {"", "."} else "./" + parent.strip("/")
    pattern = "^" + re.escape(test) + "$"
    command = (
        "go test -count=1 -json "
        + shlex.quote(package)
        + " -run "
        + shlex.quote(pattern)
    )
    return test, package, path, command


def _valid_exact_go_plan(plan: dict[str, Any]) -> bool:
    targets = plan["declared_targets"]
    if any(len(batch) != 1 for batch in plan["target_batches"]):
        return False
    for target, command, proof in zip(
        targets,
        plan["commands"],
        plan["proofs"],
        strict=True,
    ):
        binding = _exact_go_binding(target)
        if binding is None:
            return False
        test, package, test_file, expected_command = binding
        if command != expected_command or proof != {
            "kind": "go_json_test_pass",
            "test": test,
            "package": package,
            "test_file": test_file,
        }:
            return False
    return True


def _valid_dynamic_go_plan(plan: dict[str, Any]) -> bool:
    targets = plan["declared_targets"]
    if (
        plan["target_batches"] != [targets]
        or len(plan["commands"]) != 1
        or len(plan["proofs"]) != 1
        or any(not is_go_test_name(target) for target in targets)
    ):
        return False
    proof = plan["proofs"][0]
    if proof != {
        "kind": "go_json_test_pass",
        "tests": targets,
        "dynamic_discovery": True,
    }:
        return False
    return plan["commands"] == [go_test_command(targets)]


def _valid_javascript_plan(plan: dict[str, Any]) -> bool:
    if (
        plan["target_batches"] != [plan["declared_targets"]]
        or len(plan["commands"]) != 1
        or len(plan["proofs"]) != 1
    ):
        return False
    proof = plan["proofs"][0]
    if (
        not isinstance(proof, dict)
        or proof.get("kind") != "js_parser_backed_targets"
        or proof.get("targets") != plan["declared_targets"]
        or set(proof) - {
            "kind",
            "targets",
            "repo_language",
            "repo",
            "suite_module_mocks",
            "test_files",
            "target_file",
        }
    ):
        return False
    test_files = proof.get("test_files")
    declared_files = declared_js_test_files(plan["declared_targets"])
    language = proof.get("repo_language")
    repo = proof.get("repo")
    if (
        not declared_files
        or not isinstance(test_files, list)
        or not test_files
        or any(not isinstance(path, str) or not path for path in test_files)
        or len(set(test_files)) != len(test_files)
        or test_files != declared_files
        or not isinstance(language, str)
        or language not in _JAVASCRIPT_LANGUAGES
        or not isinstance(repo, str)
        or repo != repo.strip().lower()
    ):
        return False
    adapter = plan["adapter"]
    if adapter == "jest-json-verbose":
        return (
            repo not in {"nodebb/nodebb", "tutao/tutanota"}
            and "target_file" not in proof
            and plan["commands"] == [jest_test_command(test_files)]
        )
    if adapter == "mocha-json-stream":
        target_file = proof.get("target_file", "")
        return (
            repo == "nodebb/nodebb"
            and isinstance(target_file, str)
            and plan["commands"]
            == [
                mocha_test_command(
                    plan["declared_targets"],
                    test_files,
                    target_file,
                )
            ]
        )
    return (
        repo == "tutao/tutanota"
        and "target_file" not in proof
        and plan["commands"] == [tutanota_test_command(plan["declared_targets"])]
    )


def validated_test_plan_kind(
    plan: Any,
    *,
    require_commands: bool,
) -> str | None:
    """Return the trusted plan kind, or ``None`` for any malformed plan."""

    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        return None
    if plan.get("schema") != PLAN_SCHEMA:
        return None
    commands = _string_list(plan.get("commands"), allow_empty=True)
    declared = _string_list(plan.get("declared_targets"), allow_empty=True)
    target_batches = plan.get("target_batches")
    proofs = plan.get("proofs")
    if (
        commands is None
        or declared is None
        or not isinstance(target_batches, list)
        or not isinstance(proofs, list)
    ):
        return None
    if not commands:
        if require_commands:
            return None
        return EMPTY_PLAN_KIND if plan == {
            "schema": PLAN_SCHEMA,
            "adapter": "unsupported",
            "coverage": "none",
            "coverage_verified": False,
            "declared_targets": [],
            "target_batches": [],
            "commands": [],
            "proofs": [],
        } else None
    adapter = plan.get("adapter")
    if (
        adapter not in _ADAPTER_COVERAGE
        or plan.get("coverage") != _ADAPTER_COVERAGE[adapter]
        or plan.get("coverage_verified") is not True
        or not declared
        or len(set(declared)) != len(declared)
        or len(commands) != len(target_batches)
        or len(commands) != len(proofs)
        or any(not isinstance(batch, list) or not batch for batch in target_batches)
        or any(
            not isinstance(item, str) or not item
            for batch in target_batches
            for item in batch
        )
        or [item for batch in target_batches for item in batch] != declared
        or any(not isinstance(proof, dict) or not proof for proof in proofs)
        or any(not is_runnable_test_command(command) for command in commands)
    ):
        return None
    if adapter == "go-test-json":
        return adapter if _valid_exact_go_plan(plan) else None
    if adapter == "go-test-json-discovery":
        return adapter if _valid_dynamic_go_plan(plan) else None
    return adapter if _valid_javascript_plan(plan) else None


__all__ = [
    "EMPTY_PLAN_KIND",
    "NOOP_TEST_COMMANDS",
    "PLAN_SCHEMA",
    "is_runnable_test_command",
    "is_go_test_name",
    "validated_test_plan_kind",
]
