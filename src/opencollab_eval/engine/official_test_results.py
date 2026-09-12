"""Align explicit test outcomes with complete official test identities."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DURATION = re.compile(r"\s+\([\d.]+\s*m?s\)$")
VALID = {"PASSED", "FAILED", "ERROR"}


def values(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = ast.literal_eval(value)
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("invalid official target declaration")
    return value


def jest_rows(text, source):
    """Keep file, every describe level, and test title as separate ID parts."""
    result = []
    file = None
    stack = []
    details = False
    for number, original in enumerate(text.splitlines(), 1):
        line = ANSI.sub("", original)
        header = re.match(r"^(?:PASS|FAIL)\s+([^\s]+\.[cm]?[jt]sx?)(?:\s|$)", line)
        if header:
            file, stack, details = header[1], [], False
            continue
        if not file or not line.strip():
            continue
        if line.startswith("  ●") or re.match(r"^(?:Test Suites:|Tests:|Snapshots:|Time:|Ran all test)", line):
            details = True
        if details:
            continue
        test = re.match(r"^(\s+)([✓✔✕×○])\s+(.+?)\s*$", line)
        if test:
            indent = len(test[1])
            while stack and stack[-1][0] >= indent:
                stack.pop()
            name = " | ".join([file, *[x[1] for x in stack], DURATION.sub("", test[3])])
            status = "PASSED" if test[2] in "✓✔" else "FAILED" if test[2] in "✕×" else "SKIPPED"
            result.append(dict(name=name, status=status, source=source, line=number, text=original))
        elif line.startswith("  ") and not line.startswith("    at "):
            indent = len(line) - len(line.lstrip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, line.strip()))
    return result


def mocha_json_rows(document, source):
    """Read Mocha JSON reporter fullTitle identities and explicit result groups."""
    rows = []
    for key, status in [("passes", "PASSED"), ("failures", "FAILED"), ("pending", "SKIPPED")]:
        for index, test in enumerate(document.get(key, [])):
            name = test.get("fullTitle")
            if isinstance(name, str) and name:
                rows.append(dict(name=name, status=status, source=source, group=key, index=index))
    return rows


def mocha_spec_rows(text, source):
    """Read complete suite paths from Mocha spec reporter indentation."""
    result, stack = [], []
    details = False
    for number, original in enumerate(text.splitlines(), 1):
        line = ANSI.sub("", original)
        if re.match(r"^\s*\d+ (?:passing|failing|pending)(?:\s|$)", line):
            details = True
        if details or not line.strip():
            continue
        test = re.match(r"^(\s+)(?:(✓|✔|-)\s+|(\d+)\)\s+)(.+?)\s*$", line)
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if test:
            title = DURATION.sub("", test[4])
            parts = [x[1] for x in stack] + [title]
            status = "FAILED" if test[3] else "SKIPPED" if test[2] == "-" else "PASSED"
            result.append(dict(name=" ".join(parts), status=status, source=source, line=number, text=original))
        elif indent >= 2:
            stack.append((indent, line.strip()))
    return result


def reconcile(config, source_result, parsed, raw_sources, *, reporter="jest"):
    required = set(values(config.get("fail_to_pass", [])) + values(config.get("pass_to_pass", [])))
    if not required:
        raise ValueError("official target set is empty")
    by_source = defaultdict(lambda: defaultdict(list))
    for index, row in enumerate(parsed.get("tests", [])):
        if isinstance(row, dict) and row.get("name") in required:
            evidence = dict(row, source="official_parser", index=index)
            evidence["status"] = str(row.get("status", "")).upper()
            by_source["official_parser"][row["name"]].append(evidence)
    raw_count = 0
    collected_names = {
        row["name"] for row in parsed.get("tests", []) if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    for source, content in raw_sources:
        if reporter == "jest":
            rows = jest_rows(content, source)
        elif reporter == "mocha-json":
            rows = mocha_json_rows(json.loads(content), source)
        elif reporter == "mocha-spec":
            rows = mocha_spec_rows(content, source)
        else:
            raise ValueError("unsupported reporter")
        raw_count += len(rows)
        collected_names.update(row["name"] for row in rows)
        for row in rows:
            if row["name"] in required:
                by_source[source][row["name"]].append(row)
    observed, evidence, ambiguous = {}, {}, []
    for name in sorted(required):
        observations = [r for names in by_source.values() for r in names.get(name, [])]
        if not observations:
            continue
        evidence[name] = observations
        statuses = {row["status"] for row in observations}
        if len(statuses) != 1:
            ambiguous.append(name)
            continue
        observed[name] = next(iter(statuses))
    missing = sorted(required - observed.keys())
    invalid = sorted(name for name, status in observed.items() if status not in VALID)
    failed = sorted(name for name, status in observed.items() if status in {"FAILED", "ERROR"})
    reasons = set(source_result.get("technical_reasons", [])) - {"incomplete_official_target_evidence"}
    if missing or invalid or ambiguous:
        reasons.add("incomplete_official_target_evidence")
    if source_result.get("candidate_prepare_exit") != 0:
        reasons.add("candidate_preparation")
    if source_result.get("official_test_exit") not in {0, 1} or source_result.get("docker_exit") != source_result.get(
        "official_test_exit"
    ):
        reasons.add("official_execution_exit_shape")
    if source_result.get("container_absent_after") is not True:
        reasons.add("container_cleanup")
    if (
        source_result.get("candidate_identity_verified") is not True
        or source_result.get("official_tests_identity_verified") is not True
    ):
        reasons.add("source_identity_changed")
    status = "technical_failure" if reasons else "unresolved" if failed else "resolved"
    result = dict(
        source_result,
        status=status,
        resolved=status == "resolved",
        technical_reasons=sorted(reasons),
        parser_tests_collected=source_result.get("tests_collected", len(parsed.get("tests", []))),
        tests_collected=len(collected_names),
        raw_explicit_test_rows=raw_count,
        required_count=len(required),
        required_passed=sum(x == "PASSED" for x in observed.values()),
        required_failed=len(failed),
        required_missing=len(missing),
        required_ambiguous=len(ambiguous),
    )
    proof = dict(
        reporter=reporter,
        exact_test_rows=evidence,
        required_missing_tests=missing,
        required_failed_tests=failed,
        required_ambiguous_tests=ambiguous,
        required_invalid_tests=invalid,
        raw_explicit_rows=raw_count,
        model_calls=0,
    )
    return result, proof
