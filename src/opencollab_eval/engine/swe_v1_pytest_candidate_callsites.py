"""Bind pytest collection errors to fixed-test additions and candidate modules."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import unicodedata
from typing import Any

from opencollab_eval.patch_diff import patch_block_target_path, split_patch_blocks

_CALL_BINDING_TYPE_ERROR_RE = re.compile(
    r"^(?P<callable>__init__|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(\) "
    r"(?:missing \d+ required positional argument(?:s)?|"
    r"got an unexpected keyword argument|got multiple values for argument|"
    r"takes .+? positional arguments? but .+? (?:was|were) given)"
)


def candidate_call_bindings(source: str) -> list[tuple[str, str] | None] | None:
    try:
        tree = ast.parse(source)
    except (IndentationError, SyntaxError):
        return None
    bindings: list[tuple[str, str] | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            bindings.append((function.value.id, function.attr))
        elif isinstance(function, ast.Name):
            bindings.append(("", function.id))
        else:
            bindings.append(None)
    return bindings


def candidate_python_alias_paths(candidate_source_paths: list[str] | None) -> dict[str, list[str]]:
    alias_paths: dict[str, list[str]] = {}
    for raw_path in candidate_source_paths or []:
        path = pathlib.PurePosixPath(raw_path)
        alias = path.parent.name if path.name == "__init__.py" else path.stem
        if re.fullmatch(r"[A-Za-z_]\w*", alias) is None:
            return {}
        alias_paths.setdefault(alias, []).append(raw_path)
    return alias_paths


def python_test_patch_candidate_callsites(
    row: dict[str, Any],
    targets: list[str],
    candidate_source_paths: list[str],
) -> list[dict[str, Any]]:
    target_files = []
    for target in targets:
        path = str(target).split("::", 1)[0].replace("\\", "/").removeprefix("./")
        if path not in target_files:
            target_files.append(path)
    alias_paths = candidate_python_alias_paths(candidate_source_paths)
    unique_aliases = {alias for alias, paths in alias_paths.items() if len(paths) == 1}
    if not target_files or not unique_aliases:
        return []
    bindings: list[dict[str, Any]] = []
    total_bytes = 0
    for block in split_patch_blocks(str(row.get("test_patch") or "")):
        path = patch_block_target_path(block)
        normalized_path = path.replace("\\", "/").removeprefix("./")
        matched_targets = [
            target_file
            for target_file in target_files
            if normalized_path == target_file
        ]
        if len(matched_targets) != 1:
            continue
        target_file = matched_targets[0]
        for line in block:
            if not line.startswith("+") or line.startswith("+++"):
                continue
            source = line[1:].strip()
            if (
                not source
                or len(source.encode("utf-8")) > 4096
                or any(unicodedata.category(char).startswith("C") for char in source)
            ):
                continue
            call_bindings = candidate_call_bindings(source)
            if call_bindings is None:
                continue
            calls = [binding for binding in call_bindings if binding is not None]
            aliases = {alias for alias, _ in calls if alias in unique_aliases}
            if len(aliases) != 1:
                continue
            alias = next(iter(aliases))
            methods = sorted({method for call_alias, method in calls if call_alias == alias})
            binding = {
                "test_file": target_file,
                "source": source,
                "candidate_alias": alias,
                "methods": methods,
            }
            if binding in bindings:
                continue
            total_bytes += sum(
                len(str(value).encode("utf-8"))
                for value in (target_file, source, alias, *methods)
            )
            if len(bindings) >= 1024 or total_bytes > 128 * 1024:
                return []
            bindings.append(binding)
    return bindings


def pytest_plan_with_test_patch_callsites(plan: dict[str, Any], test_patch: str) -> dict[str, Any]:
    enriched = json.loads(json.dumps(plan))
    if enriched.get("adapter") != "pytest":
        return enriched
    for proof in enriched.get("proofs") or []:
        if not isinstance(proof, dict):
            continue
        bindings = python_test_patch_candidate_callsites(
            {"test_patch": test_patch},
            proof.get("targets") or [],
            proof.get("candidate_source_paths") or [],
        )
        existing = proof.get("candidate_test_callsites")
        if existing is not None and existing != bindings:
            raise ValueError("pytest candidate callsites disagree with the fixed test patch")
        if bindings and existing is None:
            proof["candidate_test_callsites"] = bindings
    return enriched


def pytest_candidate_callsite_failure_matches(
    target_files: list[str],
    log_text: str,
    candidate_source_paths: list[str],
    candidate_test_callsites: list[dict[str, Any]] | None,
) -> bool:
    alias_paths = candidate_python_alias_paths(candidate_source_paths)
    unique_aliases = {alias for alias, paths in alias_paths.items() if len(paths) == 1}
    if not unique_aliases or not candidate_test_callsites:
        return False
    valid_bindings = []
    for binding in candidate_test_callsites:
        if not isinstance(binding, dict) or set(binding) != {
            "test_file",
            "source",
            "candidate_alias",
            "methods",
        }:
            return False
        test_file = binding.get("test_file")
        source = binding.get("source")
        alias = binding.get("candidate_alias")
        methods = binding.get("methods")
        if (
            test_file not in target_files
            or not isinstance(source, str)
            or not source
            or source != source.strip()
            or len(source.encode("utf-8")) > 4096
            or alias not in unique_aliases
            or not isinstance(methods, list)
            or not methods
            or len(set(methods)) != len(methods)
            or any(re.fullmatch(r"[A-Za-z_]\w*", method) is None for method in methods)
        ):
            return False
        call_bindings = candidate_call_bindings(source)
        if call_bindings is None:
            return False
        calls = [call for call in call_bindings if call is not None]
        if (
            sorted({method for call_alias, method in calls if call_alias == alias}) != methods
            or {call_alias for call_alias, _ in calls if call_alias in unique_aliases}
            != {alias}
        ):
            return False
        valid_bindings.append(binding)
    target_pattern = "|".join(
        re.escape(path) for path in sorted(target_files, key=len, reverse=True)
    )
    frame_pattern = re.compile(
        r"(?m)^(?:.*?/)?(?P<target>"
        + target_pattern
        + r"):[0-9]+: in .+\n"
        r"(?P<source>\s+[^\n]+)\n"
        r"E\s+TypeError:\s+(?P<message>[^\n]+)$"
    )
    for match in frame_pattern.finditer(log_text):
        error = _CALL_BINDING_TYPE_ERROR_RE.match(match.group("message").strip())
        if error is None:
            continue
        callable_parts = error.group("callable").split(".")
        callable_name = callable_parts[-1]
        matches = []
        for binding in valid_bindings:
            if (
                binding["test_file"] != match.group("target")
                or binding["source"] != match.group("source").strip()
            ):
                continue
            calls = candidate_call_bindings(binding["source"])
            if not calls or any(call is None for call in calls):
                continue
            if callable_name == "__init__" and len(callable_parts) == 1:
                bound = all(call[0] == binding["candidate_alias"] for call in calls)
            else:
                expected_method = (
                    callable_parts[-2] if callable_name == "__init__" else callable_name
                )
                compatible = [call for call in calls if call[1] == expected_method]
                bound = compatible == [(binding["candidate_alias"], expected_method)]
            if bound:
                matches.append(binding)
        if len(matches) == 1:
            return True
    return False


__all__ = [
    "candidate_call_bindings",
    "candidate_python_alias_paths",
    "pytest_candidate_callsite_failure_matches",
    "pytest_plan_with_test_patch_callsites",
    "python_test_patch_candidate_callsites",
]
