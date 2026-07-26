"""Structural trust contract for executable Pro-Lite test plans."""

from __future__ import annotations

import hashlib
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
    verified_js_test_files,
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
    "runtime_dependencies",
}
_ADAPTER_COVERAGE = {
    "go-test-json": "exact_test_events",
    "go-test-json-discovery": "runtime_discovered_exact_test_events",
    "jest-json-verbose": "parser_backed_exact_targets",
    "mocha-json-stream": "parser_backed_exact_targets",
    "ospec-structured-results": "parser_backed_exact_targets",
    "pytest": "exact_targets",
}
_JAVASCRIPT_LANGUAGES = {"js", "javascript", "ts", "typescript"}
_GO_TEST_ROOT = re.compile(r"Test[A-Za-z0-9_]*")


def javascript_runtime_dependencies(adapter: str) -> list[dict[str, Any]]:
    runner = (
        "node_modules/.bin/jest"
        if adapter == "jest-json-verbose"
        else "node_modules/.bin/mocha"
        if adapter == "mocha-json-stream"
        else "node_modules"
    )
    return [
        {
            "root": "node_modules",
            "required_paths": [runner],
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


def previous_javascript_runtime_dependencies(adapter: str) -> list[dict[str, Any]]:
    """Return the file-aware dependency shape emitted before config preservation."""
    return javascript_runtime_dependencies(adapter)[:2]


def legacy_javascript_runtime_dependencies(adapter: str) -> list[dict[str, Any]]:
    """Return the exact runtime dependency shape emitted by the original v2 plan."""
    return [
        {
            "root": "node_modules",
            "required_paths": [javascript_runtime_dependencies(adapter)[0]["required_paths"][0]],
        }
    ]


def is_go_test_name(value: str) -> bool:
    if not isinstance(value, str) or not value or any(not char.isprintable() for char in value):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= 4096 and _GO_TEST_ROOT.fullmatch(value.split("/", 1)[0]) is not None


def dynamic_go_targets_supported(values: list[str]) -> bool:
    declared = set(values)
    return all(
        target.split("/", 1)[0] in declared
        or all(component for component in target.split("/")[1:])
        for target in values
    )


def is_runnable_test_command(command: str) -> bool:
    """Recognize only command families backed by an independent parser."""

    if not isinstance(command, str) or command.strip() in NOOP_TEST_COMMANDS:
        return False
    return bool(
        re.fullmatch(
            r"(?:pytest|xvfb-run -a python -m pytest --no-xvfb) "
            r"-p opencollab_pytest_proof -q -rA -o addopts= \S(?:.*\S)?",
            command,
        )
        or re.fullmatch(r"go test -count=1 -json \S+ -run \S+", command)
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


def _pytest_parameter_parent(target: str) -> str:
    node_start = target.rfind("::") + 2
    bracket = target.find("[", node_start)
    if node_start < 2 or bracket <= node_start or not target.endswith("]"):
        return ""
    return target[:bracket]


def _valid_pytest_plan(plan: dict[str, Any]) -> bool:
    if plan["coverage"] not in {"exact_targets", "parameter_parent_targets"}:
        return False
    has_parameter_fallback = False
    for batch, command, proof in zip(
        plan["target_batches"], plan["commands"], plan["proofs"], strict=True
    ):
        if proof.get("kind") != "pytest_structured_reports" or proof.get("targets") != batch:
            return False
        allowed = {
            "kind",
            "targets",
            "parameter_fallback_parents",
            "candidate_source_paths",
            "target_imports",
            "repo",
            "command_sha256",
        }
        if set(proof) - allowed:
            return False
        parents = []
        execution = []
        for target in batch:
            parent = _pytest_parameter_parent(target)
            if parent and parent not in parents:
                parents.append(parent)
            selected = parent or target
            if selected not in execution:
                execution.append(selected)
        if proof.get("parameter_fallback_parents", []) != parents:
            return False
        has_parameter_fallback = has_parameter_fallback or bool(parents)
        prefixes = (
            "pytest -p opencollab_pytest_proof -q -rA -o addopts= ",
            "xvfb-run -a python -m pytest --no-xvfb "
            "-p opencollab_pytest_proof -q -rA -o addopts= ",
        )
        if command not in {
            prefix + " ".join(shlex.quote(target) for target in execution)
            for prefix in prefixes
        }:
            return False
        digest = hashlib.sha256(
            "\0".join(shlex.split(command)).encode("utf-8")
        ).hexdigest()
        if proof.get("command_sha256") != digest:
            return False
    return has_parameter_fallback == (plan["coverage"] == "parameter_parent_targets")


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
        or not dynamic_go_targets_supported(targets)
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


def _valid_runtime_dependencies(value: list[Any]) -> bool:
    if len(value) > 16:
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) not in (
            {"root", "required_paths"},
            {"root", "required_paths", "kind", "candidate_protected"},
        ):
            return False
        root = pathlib.PurePosixPath(item.get("root") or "")
        required = item.get("required_paths")
        legacy = set(item) == {"root", "required_paths"}
        kind = "directory" if legacy else item.get("kind")
        candidate_protected = True if legacy else item.get("candidate_protected")
        if (
            not root.parts
            or root.is_absolute()
            or ".." in root.parts
            or not isinstance(required, list)
            or not required
            or len(required) > 16
            or kind not in {"directory", "file"}
            or not isinstance(candidate_protected, bool)
        ):
            return False
        for value_path in required:
            path = pathlib.PurePosixPath(value_path) if isinstance(value_path, str) else None
            if path is None or path.is_absolute() or ".." in path.parts or (path != root and root not in path.parents):
                return False
    return True


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
            "selected_test_files",
            "test_patch_files",
            "test_files",
            "target_file",
        }
    ):
        return False
    test_files = proof.get("test_files")
    declared_files = declared_js_test_files(plan["declared_targets"])
    selected_files = proof.get("selected_test_files", [])
    test_patch_files = proof.get("test_patch_files", [])
    language = proof.get("repo_language")
    repo = proof.get("repo")
    if (
        not declared_files
        or not isinstance(test_files, list)
        or not test_files
        or any(not isinstance(path, str) or not path for path in test_files)
        or len(set(test_files)) != len(test_files)
        or not isinstance(selected_files, list)
        or any(not isinstance(path, str) or not path for path in selected_files)
        or len(set(selected_files)) != len(selected_files)
        or not isinstance(test_patch_files, list)
        or any(not isinstance(path, str) or not path for path in test_patch_files)
        or len(set(test_patch_files)) != len(test_patch_files)
        or test_files != verified_js_test_files(
            plan["declared_targets"], selected_files, test_patch_files
        )
        or (test_files == declared_files)
        != (
            "selected_test_files" not in proof
            and "test_patch_files" not in proof
        )
        or not isinstance(language, str)
        or language not in _JAVASCRIPT_LANGUAGES
        or not isinstance(repo, str)
        or repo != repo.strip().lower()
    ):
        return False
    adapter = plan["adapter"]
    expected_runtime = javascript_runtime_dependencies(adapter)
    if plan["runtime_dependencies"] not in (
        expected_runtime,
        previous_javascript_runtime_dependencies(adapter),
        legacy_javascript_runtime_dependencies(adapter),
    ):
        return False
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
    runtime_dependencies = plan.get("runtime_dependencies")
    if (
        commands is None
        or declared is None
        or not isinstance(target_batches, list)
        or not isinstance(proofs, list)
        or not isinstance(runtime_dependencies, list)
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
            "runtime_dependencies": [],
        } else None
    adapter = plan.get("adapter")
    if (
        adapter not in _ADAPTER_COVERAGE
        or (
            adapter != "pytest"
            and plan.get("coverage") != _ADAPTER_COVERAGE[adapter]
        )
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
        or not _valid_runtime_dependencies(runtime_dependencies)
        or any(not is_runnable_test_command(command) for command in commands)
    ):
        return None
    if adapter == "go-test-json":
        return adapter if not runtime_dependencies and _valid_exact_go_plan(plan) else None
    if adapter == "go-test-json-discovery":
        return adapter if not runtime_dependencies and _valid_dynamic_go_plan(plan) else None
    if adapter == "pytest":
        return adapter if not runtime_dependencies and _valid_pytest_plan(plan) else None
    return adapter if _valid_javascript_plan(plan) else None


__all__ = [
    "EMPTY_PLAN_KIND",
    "NOOP_TEST_COMMANDS",
    "PLAN_SCHEMA",
    "is_runnable_test_command",
    "is_go_test_name",
    "dynamic_go_targets_supported",
    "validated_test_plan_kind",
]
