"""Strict structured proof for Go test passes and target-test build failures."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

GO_TARGET_DISCOVERY_PREFIX = "OPENCOLLAB_GO_TARGET_DISCOVERY "
_TEST_DIAGNOSTIC_RE = re.compile(
    r"(?m)(?P<path>(?:[A-Za-z]:)?[^:\r\n]*?[^/\\:\r\n]+_test\.go):"
    r"[0-9]+(?::[0-9]+)?:"
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


def _parse_go_log(log_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    events: list[dict[str, Any]] = []
    discoveries: list[dict[str, Any]] = []
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
            return None
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events, discoveries


def _package_matches(declared: str, observed: str) -> bool:
    declared = str(declared or "").replace("\\", "/").strip()
    observed = str(observed or "").replace("\\", "/").strip().rstrip("/")
    if not declared or not observed:
        return False
    if declared in {".", "./"}:
        return True
    suffix = declared.removeprefix("./").strip("/")
    return bool(suffix and (observed == suffix or observed.endswith("/" + suffix)))


def _target_file_matches(observed: str, expected: str) -> bool:
    observed_path = str(observed or "").replace("\\", "/").removeprefix("./")
    expected_path = str(expected or "").replace("\\", "/").removeprefix("./")
    if not observed_path or not expected_path:
        return False
    return observed_path == expected_path or observed_path.endswith("/" + expected_path) or (
        pathlib.PurePosixPath(observed_path).name
        == pathlib.PurePosixPath(expected_path).name
    )


def _dynamic_bindings(
    discoveries: list[dict[str, Any]],
    declared_tests: list[str],
) -> list[dict[str, Any]]:
    expected_names = {test.split("/", 1)[0] for test in declared_tests}
    bindings: list[dict[str, Any]] = []
    owners: dict[str, int] = {}
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
        ):
            return []
        for test in tests:
            owners[test] = owners.get(test, 0) + 1
        bindings.append(
            {"package": package, "tests": list(tests), "test_files": list(test_files)}
        )
    if not bindings or set(owners) != expected_names or any(count != 1 for count in owners.values()):
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
    packages = [binding["package"] for binding in bindings]
    observed = [str(event.get("Package") or "") for event in events if event.get("Package")]
    return bool(observed) and all(
        any(_package_matches(package, value) for package in packages)
        for value in observed
    )


def _dynamic_test_events_match_owners(
    events: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> bool:
    owners = {
        test: binding["package"]
        for binding in bindings
        for test in binding["tests"]
    }
    matched = False
    for event in events:
        test = event.get("Test")
        if not isinstance(test, str):
            continue
        owner = owners.get(test.split("/", 1)[0])
        if owner is None:
            continue
        matched = True
        package = event.get("Package")
        if not isinstance(package, str) or not _package_matches(owner, package):
            return False
    return matched


def _legacy_dynamic_command_matches(
    proof: dict[str, Any],
    expected_command: str,
    observed_command: str,
) -> bool:
    if (
        proof.get("dynamic_discovery") is True
        or proof.get("package")
        or expected_command != observed_command
        or not expected_command.startswith("python3 -c ")
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


def _build_output_for_package(events: list[dict[str, Any]], package: str) -> str:
    return "".join(
        str(event.get("Output") or "")
        for event in events
        if event.get("Action") == "build-output"
        and _package_matches(
            package,
            str(event.get("ImportPath") or "").split(" [", 1)[0],
        )
    )


def go_pass_proof_matches(proof: dict[str, Any], log_text: str) -> bool:
    """Require every declared Go test pass event from its planned package set."""
    declared_tests = _declared_tests(proof)
    parsed = _parse_go_log(log_text)
    if not declared_tests or parsed is None:
        return False
    events, discoveries = parsed
    if proof.get("dynamic_discovery") is True:
        bindings = _dynamic_bindings(discoveries, declared_tests)
        if (
            not bindings
            or not _events_match_bindings(events, bindings)
            or not _dynamic_test_events_match_owners(events, bindings)
        ):
            return False
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
    events, discoveries = parsed
    expected = set(declared_tests)
    exact_failures = [
        event
        for event in events
        if event.get("Action") == "fail" and event.get("Test") in expected
    ]
    if exact_failures:
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
    if any(event.get("Test") in expected for event in events):
        return False
    legacy_dynamic = _legacy_dynamic_command_matches(
        proof,
        expected_command,
        observed_command,
    )
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
    if legacy_dynamic and len(failed_packages) != 1:
        return False
    for failed_package in failed_packages:
        package_output = "".join(
            str(event.get("Output") or "")
            for event in events
            if event.get("Package") == failed_package and event.get("Action") == "output"
        )
        if "[build failed]" not in package_output:
            continue
        build_output = _build_output_for_package(events, failed_package)
        build_failed = any(
            event.get("Action") == "build-fail"
            and _package_matches(
                failed_package,
                str(event.get("ImportPath") or "").split(" [", 1)[0],
            )
            for event in events
        )
        if not build_failed:
            continue
        diagnostics = [match.group("path") for match in _TEST_DIAGNOSTIC_RE.finditer(build_output)]
        if legacy_dynamic:
            if diagnostics:
                return True
            continue
        for binding in bindings:
            if not _package_matches(binding["package"], failed_package):
                continue
            if any(
                _target_file_matches(path, target_file)
                for path in diagnostics
                for target_file in binding["test_files"]
            ):
                return True
    return False


__all__ = [
    "GO_TARGET_DISCOVERY_PREFIX",
    "go_failure_proof_matches",
    "go_pass_proof_matches",
]
