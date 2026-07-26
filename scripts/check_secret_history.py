#!/usr/bin/env python3
"""Reject high-confidence secrets introduced after a trusted Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_BASELINE_PATH = ".secrets.baseline"
_DETECT_SECRETS_VERSION = "1.5.0"
_APPROVED_BASELINE_SHA256 = {
    "".join(
        (
            "2ab9e49f",
            "1de09f67",
            "c8840583",
            "37c79f8e",
            "5e8c2089",
            "105c6812",
            "86df5bb9",
            "679e0e01",
        )
    )
}
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
            rb"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
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


def _tree_file(repository: Path, commit: str, path: str) -> bytes | None:
    output = _git(repository, "ls-tree", "-z", commit, "--", path)
    if not output:
        return None
    entries = [entry for entry in output.split(b"\0") if entry]
    if len(entries) != 1:
        raise ValueError(f"expected one {path!r} entry in commit {commit}")
    metadata, raw_path = entries[0].split(b"\t", 1)
    _mode, object_type, object_id = metadata.decode("ascii").split()
    if os.fsdecode(raw_path) != path or object_type != "blob":
        raise ValueError(f"{path!r} is not a regular file in commit {commit}")
    return _git(repository, "cat-file", "blob", object_id)


def _baseline_document(content: bytes, *, require_audit: bool) -> dict:
    try:
        baseline = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the secret baseline is not valid UTF-8 JSON") from exc
    if not isinstance(baseline, dict) or baseline.get("version") != _DETECT_SECRETS_VERSION:
        raise ValueError("the secret baseline must use detect-secrets 1.5.0")
    if not isinstance(baseline.get("plugins_used"), list) or not baseline["plugins_used"]:
        raise ValueError("the secret baseline must declare its scanner plugins")
    results = baseline.get("results")
    if not isinstance(results, dict):
        raise ValueError("the secret baseline must contain a results mapping")
    for path, findings in results.items():
        if not isinstance(path, str) or not isinstance(findings, list):
            raise ValueError("the secret baseline has an invalid results entry")
        if require_audit and any(
            not isinstance(finding, dict) or finding.get("is_secret") is not False
            for finding in findings
        ):
            raise ValueError("every baseline finding must have an audited false verdict")
    baseline.pop("generated_at", None)
    return baseline


def _baseline_digest(content: bytes) -> str:
    canonical = json.dumps(
        _baseline_document(content, require_audit=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _baseline_identities(document: dict) -> set[tuple[str, str, str]]:
    identities: set[tuple[str, str, str]] = set()
    for path, findings in document["results"].items():
        for finding in findings:
            detector = finding.get("type")
            fingerprint = finding.get("hashed_secret")
            if not isinstance(detector, str) or not isinstance(fingerprint, str):
                raise ValueError("the secret baseline has an invalid finding identity")
            identities.add((path, detector, fingerprint))
    return identities


def _check_baseline_change(
    repository: Path,
    base: str,
    head: str,
) -> bytes | None:
    head_content = _tree_file(repository, head, _BASELINE_PATH)
    if head_content is None:
        print("::error::.secrets.baseline is missing from the proposed tree.")
        return None
    head_digest = _baseline_digest(head_content)
    base_content = (
        None if base == _ZERO_SHA else _tree_file(repository, base, _BASELINE_PATH)
    )
    if base_content != head_content and head_digest not in _APPROVED_BASELINE_SHA256:
        print(
            "::error::.secrets.baseline changed without a digest approved by "
            "the trusted base checker."
        )
        return None
    return head_content


def _detect_secrets_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH"):
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    return environment


def _verify_detect_secrets() -> None:
    completed = subprocess.run(
        ["detect-secrets", "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=_detect_secrets_environment(),
    )
    if completed.stdout.strip() != _DETECT_SECRETS_VERSION:
        raise ValueError(
            f"detect-secrets {_DETECT_SECRETS_VERSION} is required, "
            f"found {completed.stdout.strip()!r}"
        )


def _detect_secrets_identities(
    repository: Path,
    commit: str,
    baseline_content: bytes,
) -> set[tuple[str, str, str]]:
    with tempfile.TemporaryDirectory(prefix="opencollab-secret-tree-") as temporary:
        root = Path(temporary)
        tree = root / "tree"
        tree.mkdir()
        for relative, object_id in _tree_blobs(repository, commit):
            if relative == _BASELINE_PATH:
                continue
            target = tree.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_git(repository, "cat-file", "blob", object_id))
        baseline = root / "baseline.json"
        baseline.write_bytes(baseline_content)
        subprocess.run(
            [
                "detect-secrets",
                "scan",
                "--all-files",
                "--baseline",
                str(baseline),
            ],
            cwd=tree,
            check=True,
            capture_output=True,
            env=_detect_secrets_environment(),
        )
        observed = _baseline_document(
            baseline.read_bytes(),
            require_audit=False,
        )
    return _baseline_identities(observed)


def _scan_tree_with_detect_secrets(
    repository: Path,
    commit: str,
    baseline_content: bytes,
    trusted_identities: set[tuple[str, str, str]],
) -> bool:
    expected = _baseline_document(baseline_content, require_audit=True)
    allowed_identities = _baseline_identities(expected) | trusted_identities
    observed_identities = _detect_secrets_identities(
        repository,
        commit,
        baseline_content,
    )
    if not observed_identities <= allowed_identities:
        print(
            "::error::detect-secrets found an unaudited finding in commit "
            f"{commit}."
        )
        return False
    return True


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
    baseline_content = _check_baseline_change(
        repository,
        resolved_base,
        resolved_head,
    )
    if baseline_content is None:
        return 1
    _verify_detect_secrets()
    trusted_detect_identities = (
        set()
        if base == _ZERO_SHA
        else _detect_secrets_identities(
            repository,
            resolved_base,
            baseline_content,
        )
    )
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
        if not _scan_tree_with_detect_secrets(
            repository,
            commit,
            baseline_content,
            trusted_detect_identities,
        ):
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
