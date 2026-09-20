"""Identify a missing test dependency in an otherwise bound Go test package."""

from __future__ import annotations

import re
from typing import Any

_MISSING_MODULE = re.compile(
    r"(?m)^(?P<path>[^:\r\n]+\.go):[0-9]+(?::[0-9]+)?: "
    r"no required module provides package (?P<module>[^;\s]+);"
)
_DIAGNOSTIC = re.compile(r"(?m)^[^:\r\n]+\.go:[0-9]+(?::[0-9]+)?:[^\r\n]+")


def bound_test_dependency_unavailable(
    events: list[dict[str, Any]], package: str, test_files: list[str]
) -> bool:
    failures = [
        event for event in events
        if event.get("Action") == "fail" and event.get("Package") == package
        and not event.get("Test") and isinstance(event.get("FailedBuild"), str)
    ]
    if len(failures) != 1:
        return False
    dependency = failures[0]["FailedBuild"]
    if not any(event.get("Action") == "build-fail" and event.get("ImportPath") == dependency for event in events):
        return False
    output = "".join(
        str(event.get("Output") or "") for event in events
        if event.get("Action") == "build-output" and event.get("ImportPath") == dependency
    )
    matches = list(_MISSING_MODULE.finditer(output))
    diagnostics = list(_DIAGNOSTIC.finditer(output))
    allowed = {path.removeprefix("./") for path in test_files}
    return bool(matches) and len(matches) == len(diagnostics) and all(
        match.group("module") == dependency and match.group("path").removeprefix("./") in allowed
        for match in matches
    )
