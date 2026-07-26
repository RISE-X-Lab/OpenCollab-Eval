#!/usr/bin/env python3
"""Reject high-confidence secrets introduced after a trusted Git commit."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_ZERO_SHA = "0" * 40
_PATTERNS = (
    (
        "private key",
        re.compile(
            rb"-----BEGIN (?:PGP PRIVATE KEY BLOCK|"
            rb"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY)-----"
        ),
    ),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    (
        "GitHub fine-grained token",
        re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ),
    ("GitLab token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(rb"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    (
        "OpenAI API key",
        re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    ("Hugging Face token", re.compile(rb"\bhf_[A-Za-z0-9]{30,}\b")),
    (
        "credential-bearing URL",
        re.compile(
            rb"\b[a-z][a-z0-9+.-]{1,15}://"
            rb"[^/\s:@]{1,128}:[^/\s:@]{8,128}@",
            re.IGNORECASE,
        ),
    ),
    (
        "assigned credential",
        re.compile(
            rb"(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
            rb"client[_-]?secret|password|passwd)\b"
            rb"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{16,})",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    """One location and fingerprint produced by a high-confidence detector."""

    detector: str
    path: str
    fingerprint: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.detector, self.path, self.fingerprint


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        env=_git_environment(),
    ).stdout


def _resolve_commit(repository: Path, revision: str) -> str:
    return (
        _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
        .decode("ascii")
        .strip()
    )


def _commits(repository: Path, base: str, head: str) -> list[str]:
    if base == _ZERO_SHA:
        output = _git(repository, "rev-list", "--reverse", head)
    else:
        output = _git(repository, "rev-list", "--reverse", f"{base}..{head}")
    return output.decode("ascii").split()


def _tree_blobs(repository: Path, commit: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    output = _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit)
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        _mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        relative = os.fsdecode(raw_path)
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError(f"unsafe Git path: {relative!r}")
        entries.append((relative, object_id))
    return entries


def _scan_blob(content: bytes) -> list[tuple[str, str, int]]:
    matches: list[tuple[str, str, int]] = []
    for detector, pattern in _PATTERNS:
        for match in pattern.finditer(content):
            secret = match.group(1) if match.lastindex else match.group(0)
            fingerprint = hashlib.sha256(secret).hexdigest()
            line = content.count(b"\n", 0, match.start()) + 1
            matches.append((detector, fingerprint, line))
    return matches


def _scan_tree(
    repository: Path,
    commit: str,
    cache: dict[str, list[tuple[str, str, int]]],
) -> list[Finding]:
    blobs = _tree_blobs(repository, commit)
    if not blobs:
        raise ValueError(f"commit {commit} has no files to scan")
    findings: list[Finding] = []
    for relative, object_id in blobs:
        if object_id not in cache:
            content = _git(repository, "cat-file", "blob", object_id)
            cache[object_id] = _scan_blob(content)
        findings.extend(
            Finding(detector, relative, fingerprint, line)
            for detector, fingerprint, line in cache[object_id]
        )
    return findings


def _command_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def check_secret_history(repository: Path, base: str, head: str) -> int:
    """Scan every proposed commit against findings from the trusted base."""
    resolved_head = _resolve_commit(repository, head)
    resolved_base = base
    cache: dict[str, list[tuple[str, str, int]]] = {}
    trusted_counts: Counter[tuple[str, str, str]] = Counter()
    if base != _ZERO_SHA:
        resolved_base = _resolve_commit(repository, base)
        trusted_counts.update(
            finding.identity
            for finding in _scan_tree(repository, resolved_base, cache)
        )

    commits = _commits(repository, resolved_base, resolved_head)
    if not commits:
        print("::error::No proposed commits were available for the secret scan.")
        return 1
    for commit in commits:
        observed_counts: Counter[tuple[str, str, str]] = Counter()
        introduced: list[Finding] = []
        for finding in _scan_tree(repository, commit, cache):
            observed_counts[finding.identity] += 1
            if observed_counts[finding.identity] > trusted_counts[finding.identity]:
                introduced.append(finding)
        introduced.sort(
            key=lambda finding: (
                os.fsencode(finding.path),
                finding.line,
                finding.detector,
            ),
        )
        if introduced:
            for finding in introduced:
                print(
                    f"::error file={_command_property(finding.path)},"
                    f"line={finding.line}::Potential {finding.detector} "
                    f"introduced in commit {commit}."
                )
            return 1
    print("Secret history checks passed.")
    return 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        return check_secret_history(Path.cwd(), args.base, args.head)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"::error::Secret history scan failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
