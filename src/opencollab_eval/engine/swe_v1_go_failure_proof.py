"""Strict structured proof for Go test passes and target-test build failures."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

GO_TARGET_DISCOVERY_PREFIX = "OPENCOLLAB_GO_TARGET_DISCOVERY "
_GO_DIAGNOSTIC_RE = re.compile(
    r"(?m)(?P<path>(?:[A-Za-z]:)?[^:\r\n]*?[^/\\:\r\n]+\.go):"
    r"[0-9]+(?::[0-9]+)?:"
)
_PLAIN_GO_DIAGNOSTIC_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^:\r\n]*?[^/\\:\r\n]+\.go):"
    r"[0-9]+(?::[0-9]+)?:[^\r\n]+\Z"
)
_GO_BUILD_HEADER_RE = re.compile(
    r"# (?P<package>\S+) \[(?P<test_package>\S+)\.test\]\Z"
)
_GO_DEPENDENCY_BUILD_HEADER_RE = re.compile(r"# (?P<package>\S+)\Z")
_PLAIN_PACKAGE_FAILURE_RE = re.compile(
    r"FAIL\s+(?P<package>\S+)\s+\[(?:build|setup) failed\]\Z"
)


def _declared_tests(proof: dict[str, Any]) -> list[str]:
    values = proof.get("tests")
    if values is None:
        values = [proof.get("test")]
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
    ):
        return []
    return values


def _parse_go_log(
    log_text: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[tuple[str, str]],
    list[str],
] | None:
    events: list[dict[str, Any]] = []
    discoveries: list[dict[str, Any]] = []
    plain_diagnostics: list[tuple[str, str]] = []
    build_headers: list[str] = []
    current_build_header = ""
    dependency_output = False
    for raw_line in str(log_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(GO_TARGET_DISCOVERY_PREFIX):
            payload = line[len(GO_TARGET_DISCOVERY_PREFIX) :]
            try:
                discovery = json.loads(payload)
            except json.JSONDecodeError:
                return None
            if not isinstance(discovery, dict):
                return None
            discoveries.append(discovery)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            diagnostic = _PLAIN_GO_DIAGNOSTIC_RE.fullmatch(line)
            header = _GO_BUILD_HEADER_RE.fullmatch(line)
            dependency_header = _GO_DEPENDENCY_BUILD_HEADER_RE.fullmatch(line)
            plain_failure = _PLAIN_PACKAGE_FAILURE_RE.fullmatch(line)
            if diagnostic is not None:
                plain_diagnostics.append(
                    (current_build_header, diagnostic.group("path"))
                )
                if current_build_header and current_build_header not in build_headers:
                    build_headers.append(current_build_header)
            elif header is not None and header.group("package") in {
                header.group("test_package"),
                header.group("test_package") + "_test",
            }:
                current_build_header = header.group("test_package")
                build_headers.append(current_build_header)
                dependency_output = True
            elif dependency_header is not None:
                current_build_header = dependency_header.group("package")
                dependency_output = True
            elif plain_failure is not None:
                package = plain_failure.group("package")
                events.extend(
                    (
                        {
                            "Action": "output",
                            "Package": package,
                            "Output": line + "\n",
                        },
                        {"Action": "fail", "Package": package},
                    )
                )
                dependency_output = False
            elif dependency_output:
                continue
            else:
                return None
            continue
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events, discoveries, plain_diagnostics, build_headers


def _package_matches(declared: str, observed: str) -> bool:
    declared = str(declared or "").replace("\\", "/").strip()
    observed = str(observed or "").replace("\\", "/").strip().rstrip("/")
    if not declared or not observed:
        return False
    if declared in {".", "./"}:
        return True
    suffix = declared.removeprefix("./").strip("/")
    return bool(suffix and (observed == suffix or observed.endswith("/" + suffix)))


def _diagnostic_belongs_to_package(observed: str, package: str) -> bool:
    observed_path = str(observed or "").replace("\\", "/").removeprefix("./")
    package_path = str(package or "").replace("\\", "/").removeprefix("./").strip("/")
    if not observed_path or not observed_path.endswith("_test.go"):
        return False
    observed_parent = pathlib.PurePosixPath(observed_path).parent.as_posix().rstrip("/")
    if not package_path or package_path == ".":
        return observed_parent in {"", "."}
    return observed_parent == package_path or observed_parent.endswith("/" + package_path)


def _candidate_diagnostic_belongs_to_package(
    observed: str,
    package: str,
    candidate_source_paths: list[str],
) -> bool:
    normalized = str(observed or "").replace("\\", "/").removeprefix("./")
    matches = [
        path
        for path in candidate_source_paths
        if normalized == path or normalized.endswith("/" + path)
    ]
    if len(matches) != 1 or not matches[0].endswith(".go"):
        return False
    parent = pathlib.PurePosixPath(matches[0]).parent.as_posix()
    package_path = str(package or "").replace("\\", "/").removeprefix("./").strip("/")
    return package_path in {"", "."} and parent in {"", "."} or bool(
        package_path
        and (parent == package_path or parent.endswith("/" + package_path))
    )


def _legacy_diagnostic_belongs_to_package(
    observed: str,
    package: str,
    candidate_source_paths: list[str],
) -> bool:
    normalized = str(observed or "").replace("\\", "/").removeprefix("./")
    parent = pathlib.PurePosixPath(normalized).parent.as_posix().strip("/")
    package_path = str(package or "").replace("\\", "/").removeprefix("./").strip("/")
    package_matches = parent in {"", "."} or bool(
        package_path == parent or package_path.endswith("/" + parent)
    )
    if not package_matches:
        return False
    if normalized.endswith("_test.go"):
        return True
    return any(
        normalized == path or normalized.endswith("/" + path)
        for path in candidate_source_paths
    )


def _dynamic_bindings(
    discoveries: list[dict[str, Any]],
    declared_tests: list[str],
) -> list[dict[str, Any]]:
    expected_names = set(declared_tests)
    bindings: list[dict[str, Any]] = []
    owners: set[str] = set()
    packages: set[str] = set()
    for discovery in discoveries:
        package = discovery.get("package")
        tests = discovery.get("tests")
        test_files = discovery.get("test_files")
        if (
            not isinstance(package, str)
            or not package
            or not isinstance(tests, list)
            or not tests
            or any(not isinstance(test, str) or test not in expected_names for test in tests)
            or not isinstance(test_files, list)
            or not test_files
            or any(
                not isinstance(path, str) or not path.endswith("_test.go")
                for path in test_files
            )
            or package in packages
        ):
            return []
        packages.add(package)
        owners.update(tests)
        bindings.append(
            {"package": package, "tests": list(tests), "test_files": list(test_files)}
        )
    if not bindings or owners != expected_names:
        return []
    return bindings


def _proof_bindings(
    proof: dict[str, Any],
    discoveries: list[dict[str, Any]],
    declared_tests: list[str],
) -> list[dict[str, Any]]:
    if proof.get("dynamic_discovery") is True:
        return _dynamic_bindings(discoveries, declared_tests)
    if discoveries:
        return []
    package = proof.get("package")
    test_file = proof.get("test_file")
    if not isinstance(package, str) or not package:
        return []
    if not isinstance(test_file, str) or not test_file.endswith("_test.go"):
        return []
    return [{"package": package, "tests": declared_tests, "test_files": [test_file]}]


def _events_match_bindings(
    events: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> bool:
    observed = [str(event.get("Package") or "") for event in events if event.get("Package")]
    return bool(observed) and all(_matching_binding(bindings, value) for value in observed)


def _matching_binding(
    bindings: list[dict[str, Any]],
    observed_package: str,
) -> dict[str, Any] | None:
    matches = [
        binding
        for binding in bindings
        if _package_matches(binding["package"], observed_package)
    ]
    return matches[0] if len(matches) == 1 else None


def _dynamic_test_events_match_owners(
    events: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> bool:
    matched = False
    for event in events:
        test = event.get("Test")
        if not isinstance(test, str):
            continue
        if not any(test in binding["tests"] for binding in bindings):
            continue
        matched = True
        package = event.get("Package")
        binding = _matching_binding(bindings, package) if isinstance(package, str) else None
        if binding is None or test not in binding["tests"]:
            return False
    return matched


def _dynamic_passes_cover_bindings(
    events: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> bool:
    expected = {
        (binding["package"], test)
        for binding in bindings
        for test in binding["tests"]
    }
    passed: set[tuple[str, str]] = set()
    for event in events:
        test = event.get("Test")
        package = event.get("Package")
        if event.get("Action") != "pass" or not isinstance(test, str):
            continue
        binding = _matching_binding(bindings, package) if isinstance(package, str) else None
        if binding is not None and test in binding["tests"]:
            passed.add((binding["package"], test))
    return passed == expected


def _legacy_dynamic_command_matches(
    proof: dict[str, Any],
    expected_command: str,
    observed_command: str,
) -> bool:
    if (
        proof.get("dynamic_discovery") is True
        or proof.get("package")
        or expected_command != observed_command
        or not expected_command.startswith(("python3 -c ", "python3 -I -c "))
    ):
        return False
    required_fragments = (
        'for path in pathlib.Path(".").rglob("*_test.go")',
        'if re.search(r"(?m)^func\\s+" + re.escape(name) + r"\\s*\\(", text)',
        'print("unable to map Go tests to packages: "',
        'subprocess.run(["go", "test", "-count=1", "-json", package, "-run", pattern])',
    )
    declared_tests = _declared_tests(proof)
    return bool(
        declared_tests
        and all(fragment in expected_command for fragment in required_fragments)
        and all(json.dumps(test) in expected_command for test in declared_tests)
    )


def _build_unit_matches(package: str, import_path: str) -> bool:
    build_package = import_path.split(" [", 1)[0]
    return _package_matches(package, build_package) or (
        build_package.endswith("_test")
        and _package_matches(package, build_package.removesuffix("_test"))
    )


def _build_output_for_package(events: list[dict[str, Any]], package: str) -> str:
    return "".join(
        str(event.get("Output") or "")
        for event in events
        if event.get("Action") == "build-output"
        and _build_unit_matches(package, str(event.get("ImportPath") or ""))
    )


def _target_timeout_matches(
    proof: dict[str, Any],
    events: list[dict[str, Any]],
    discoveries: list[dict[str, Any]],
    declared_tests: list[str],
    *,
    expected_command: str,
    observed_command: str,
) -> bool:
    if proof.get("dynamic_discovery") is True and (
        not expected_command or expected_command != observed_command
    ):
        return False
    bindings = _proof_bindings(proof, discoveries, declared_tests)
    if not bindings or not _events_match_bindings(events, bindings):
        return False
    failed_packages = {
        str(event.get("Package") or "")
        for event in events
        if event.get("Action") == "fail" and event.get("Package")
    }
    for binding in bindings:
        matching_packages = [
            package
            for package in failed_packages
            if _package_matches(binding["package"], package)
        ]
        if len(matching_packages) != 1:
            continue
        package = matching_packages[0]
        package_output = "".join(
            str(event.get("Output") or "")
            for event in events
            if event.get("Package") == package and event.get("Action") == "output"
        )
        if "panic: test timed out after " not in package_output:
            continue
        for test in binding["tests"]:
            actions = {
                str(event.get("Action") or "")
                for event in events
                if event.get("Package") == package and event.get("Test") == test
            }
            ran = any(
                event.get("Action") == "run"
                and event.get("Test") == test
                and event.get("Package") == package
                for event in events
            )
            target_output = "".join(
                str(event.get("Output") or "")
                for event in events
                if event.get("Action") == "output"
                and event.get("Package") == package
                and event.get("Test") == test
            )
            if ran and actions.isdisjoint({"pass", "fail", "skip"}) and test in target_output:
                return True
    return False


def go_pass_proof_matches(proof: dict[str, Any], log_text: str) -> bool:
    """Require every declared Go test pass event from its planned package set."""
    declared_tests = _declared_tests(proof)
    parsed = _parse_go_log(log_text)
    if not declared_tests or parsed is None:
        return False
    events, discoveries, plain_diagnostics, build_headers = parsed
    if plain_diagnostics or build_headers:
        return False
    if proof.get("dynamic_discovery") is True:
        bindings = _dynamic_bindings(discoveries, declared_tests)
        if (
            not bindings
            or not _events_match_bindings(events, bindings)
            or not _dynamic_test_events_match_owners(events, bindings)
        ):
            return False
        return _dynamic_passes_cover_bindings(events, bindings)
    elif discoveries:
        return False
    elif proof.get("package"):
        bindings = _proof_bindings(proof, discoveries, declared_tests)
        if not bindings or not _events_match_bindings(events, bindings):
            return False
    expected = set(declared_tests)
    passed = {
        event["Test"]
        for event in events
        if event.get("Action") == "pass" and event.get("Test") in expected
    }
    return passed == expected


def go_failure_proof_matches(
    proof: dict[str, Any],
    log_text: str,
    *,
    expected_command: str = "",
    observed_command: str = "",
) -> bool:
    """Accept an exact test failure or a compiler failure bound to its target test."""
    declared_tests = _declared_tests(proof)
    parsed = _parse_go_log(log_text)
    if not declared_tests or parsed is None:
        return False
    events, discoveries, plain_diagnostics, build_headers = parsed
    candidate_source_paths = [
        str(path).replace("\\", "/").removeprefix("./")
        for path in proof.get("candidate_source_paths") or []
        if isinstance(path, str)
    ]
    expected = set(declared_tests)
    exact_failures = [
        event
        for event in events
        if event.get("Action") == "fail" and event.get("Test") in expected
    ]
    if exact_failures:
        if plain_diagnostics or build_headers:
            return False
        if proof.get("dynamic_discovery") is not True:
            if discoveries:
                return False
            if not proof.get("package"):
                return True
            bindings = _proof_bindings(proof, discoveries, declared_tests)
            return bool(
                bindings
                and all(
                    event.get("Package")
                    and any(
                        _package_matches(binding["package"], event["Package"])
                        for binding in bindings
                    )
                    for event in exact_failures
                )
            )
        bindings = _dynamic_bindings(discoveries, declared_tests)
        return bool(
            bindings
            and _events_match_bindings(events, bindings)
            and _dynamic_test_events_match_owners(events, bindings)
        )
    if _target_timeout_matches(
        proof,
        events,
        discoveries,
        declared_tests,
        expected_command=expected_command,
        observed_command=observed_command,
    ):
        return True
    legacy_dynamic = _legacy_dynamic_command_matches(
        proof,
        expected_command,
        observed_command,
    )
    if proof.get("dynamic_discovery") is True and (
        not expected_command or expected_command != observed_command
    ):
        return False
    bindings = _proof_bindings(proof, discoveries, declared_tests)
    if not bindings:
        if not legacy_dynamic or discoveries:
            return False
        bindings = []
    elif not _events_match_bindings(events, bindings):
        return False
    failed_packages = {
        str(event.get("Package") or "")
        for event in events
        if event.get("Action") == "fail" and event.get("Package")
    }
    if plain_diagnostics and (
        not expected_command
        or expected_command != observed_command
    ):
        return False
    if build_headers:
        unique_headers = set(build_headers)
        if len(unique_headers) != len(failed_packages) or any(
            sum(_package_matches(failed_package, header) for header in unique_headers) != 1
            for failed_package in failed_packages
        ) or any(
            sum(_package_matches(failed_package, header) for failed_package in failed_packages) != 1
            for header in unique_headers
        ):
            return False
    if legacy_dynamic and len(failed_packages) != 1:
        return False
    if legacy_dynamic:
        failed_package = next(iter(failed_packages))
        package_output = "".join(
            str(event.get("Output") or "")
            for event in events
            if event.get("Package") == failed_package and event.get("Action") == "output"
        )
        build_output = _build_output_for_package(events, failed_package)
        diagnostics = [
            match.group("path")
            for match in _GO_DIAGNOSTIC_RE.finditer(build_output)
        ]
        return bool(
            "[build failed]" in package_output
            and any(
                event.get("Action") == "build-fail"
                and _package_matches(
                    failed_package,
                    str(event.get("ImportPath") or "").split(" [", 1)[0],
                )
                for event in events
            )
            and diagnostics
            and all(
                _legacy_diagnostic_belongs_to_package(
                    path,
                    failed_package,
                    candidate_source_paths,
                )
                for path in diagnostics
            )
        )
    proven_packages = []
    for failed_package in failed_packages:
        matching_bindings = [
            binding
            for binding in bindings
            if _package_matches(binding["package"], failed_package)
        ]
        if len(matching_bindings) != 1:
            return False
        binding = matching_bindings[0]
        if any(
            event.get("Test") in binding["tests"]
            and isinstance(event.get("Package"), str)
            and _package_matches(binding["package"], event["Package"])
            for event in events
        ):
            return False
        package_output = "".join(
            str(event.get("Output") or "")
            for event in events
            if event.get("Package") == failed_package and event.get("Action") == "output"
        )
        if not any(
            marker in package_output
            for marker in ("[build failed]", "[setup failed]")
        ):
            return False
        if plain_diagnostics:
            matching_headers = [
                header
                for header in build_headers
                if _package_matches(failed_package, header)
            ]
            if len(matching_headers) != 1:
                return False
            diagnostics = [
                path
                for header, path in plain_diagnostics
                if header == matching_headers[0]
            ]
        else:
            build_output = _build_output_for_package(events, failed_package)
            build_failed = any(
                event.get("Action") == "build-fail"
                and _build_unit_matches(
                    failed_package,
                    str(event.get("ImportPath") or ""),
                )
                for event in events
            )
            if not build_failed:
                return False
            diagnostics = [match.group("path") for match in _GO_DIAGNOSTIC_RE.finditer(build_output)]
        if not diagnostics or not all(
            _diagnostic_belongs_to_package(path, binding["package"])
            or _candidate_diagnostic_belongs_to_package(
                path,
                binding["package"],
                candidate_source_paths,
            )
            for path in diagnostics
        ):
            return False
        proven_packages.append(failed_package)
    return bool(proven_packages)


__all__ = [
    "GO_TARGET_DISCOVERY_PREFIX",
    "go_failure_proof_matches",
    "go_pass_proof_matches",
]
