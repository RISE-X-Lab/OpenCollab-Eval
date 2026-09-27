"""Attribute a target's failed dependency build to changed candidate source."""

from __future__ import annotations

import pathlib
import re

_DIAGNOSTIC = re.compile(r"(?m)^(?P<path>[^:\r\n]+\.go):[0-9]+(?::[0-9]+)?:")


def candidate_dependency_build_failed(events, bindings, target, candidate_paths):
    """Require Go's explicit FailedBuild link and owned source diagnostics."""
    links = {
        event.get("FailedBuild")
        for event in events
        if event.get("Action") == "fail" and event.get("Package") == target
        and isinstance(event.get("FailedBuild"), str)
    }
    if len(links) != 1:
        return False
    dependency = links.pop()
    if dependency == target:
        return False
    roots = []
    for binding in bindings:
        relative = str(binding["package"]).removeprefix("./").rstrip("/")
        if relative in {"", "."}:
            roots.append(target)
        elif target.endswith("/" + relative):
            roots.append(target[: -len(relative) - 1])
    if len(roots) != 1 or not dependency.startswith(roots[0] + "/"):
        return False
    dependency_directory = dependency[len(roots[0]) + 1:]
    if not any(event.get("Action") == "build-fail" and event.get("ImportPath") == dependency
               for event in events):
        return False
    output = "".join(str(event.get("Output") or "") for event in events
                     if event.get("Action") == "build-output" and event.get("ImportPath") == dependency)
    diagnostics = [match.group("path").removeprefix("./") for match in _DIAGNOSTIC.finditer(output)]
    return bool(diagnostics) and all(
        path in candidate_paths
        and pathlib.PurePosixPath(path).parent.as_posix() == dependency_directory
        for path in diagnostics
    )
