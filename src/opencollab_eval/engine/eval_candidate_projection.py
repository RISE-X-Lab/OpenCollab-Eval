"""Build and verify the candidate tree used by a fresh evaluation workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from opencollab_eval.engine.swe_eval_record_identity import (
        canonical_sha256,
        sha256_equal,
    )
else:
    from swe_eval_record_identity import canonical_sha256, sha256_equal

if __package__:
    from opencollab_eval.generation.gen_prediction_snapshot_support import anonymous_commit_oid
else:
    from opencollab_snapshot_support import anonymous_commit_oid

EXPECTATION_SCHEMA = "opencollab.eval_candidate_expectation.v1"
SOURCE_PROJECTION_SCHEMA = "opencollab.eval_candidate_source_projection.v1"
PROJECTION_SCHEMA = "opencollab.eval_candidate_projection.v2"
PROJECTION_FAILURE_SCHEMA = "opencollab.eval_candidate_projection_failure.v1"
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_IDENTITY_KEYS = frozenset({"run_identity_sha256", "source_patch_sha256", "eval_patch_sha256"})
_IDENTITY_KEYS = (
    "instance_id", "record_id", "run_identity_sha256", "source_patch_sha256",
    "eval_patch_sha256", "source_base_commit", "source_anonymous_base", "source_base_tree",
    "source_candidate_tree", "expected_candidate_tree",
)
_EXPECTATION_KEYS = {"schema", *_IDENTITY_KEYS}
_SOURCE_KEYS = {
    "schema", "status", "verified_source_base_commit",
    "verified_source_anonymous_base", "verified_source_base_tree",
    "verified_source_candidate_tree", "generation_tree_matches", *_IDENTITY_KEYS,
}
_V1_KEYS = {
    "schema", "status", "base_commit", "base_tree", "candidate_tree",
    "generation_tree_matches", *_IDENTITY_KEYS,
}
_V2_KEYS = {
    "schema", "status", "source_projection_sha256", "verified_source_base_commit",
    "verified_source_anonymous_base", "verified_source_base_tree",
    "verified_source_candidate_tree", "prepared_base_commit", "prepared_base_tree",
    "prepared_candidate_tree", "worktree_candidate_tree", "generation_tree_matches",
    "official_worktree_matches", *_IDENTITY_KEYS,
}
_FAILURE_KEYS = {
    "schema", "status", "error_kind", "phase", "instance_id", "record_id",
    "run_identity_sha256", "source_patch_sha256", "eval_patch_sha256",
    "source_base_commit", "source_anonymous_base", "source_base_tree",
    "source_candidate_tree", "expected_candidate_tree",
    "verified_base_commit", "verified_base_tree", "source_projection_sha256",
}


def _identity_value_equal(key: str, left: object, right: object) -> bool:
    if key in _SHA256_IDENTITY_KEYS:
        return sha256_equal(left, right)
    return left == right


def _identity_values_match(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return all(
        _identity_value_equal(key, left.get(key), right.get(key))
        for key in _IDENTITY_KEYS
    )


class CandidateProjectionError(RuntimeError):
    """Raised when a patch cannot be bound to its declared evaluation base."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "projection_runtime_error",
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.context = dict(context or {})


def candidate_projection_failure_valid(
    report: object,
    expectation: object,
    *,
    source_projection: object | None = None,
    base_commit: str = "",
    base_tree: str = "",
) -> bool:
    """Validate a bound patch-not-applicable result from trusted projection."""
    if (
        not isinstance(report, dict)
        or not _expectation_valid(expectation)
        or set(report) != _FAILURE_KEYS
        or report.get("schema") != PROJECTION_FAILURE_SCHEMA
        or report.get("status") != "failed"
        or report.get("error_kind") != "patch_not_applicable"
        or report.get("phase") not in {"source", "prepared"}
        or not _identity_values_match(report, expectation)
    ):
        return False
    verified_commit = str(report.get("verified_base_commit") or "")
    verified_tree = str(report.get("verified_base_tree") or "")
    if (
        _OID_RE.fullmatch(verified_commit) is None
        or _OID_RE.fullmatch(verified_tree) is None
        or len(verified_commit) != len(verified_tree)
    ):
        return False
    if report["phase"] == "source":
        return bool(
            report.get("source_projection_sha256") == ""
            and _OID_RE.fullmatch(base_commit)
            and _OID_RE.fullmatch(base_tree)
            and verified_commit == base_commit
            and verified_tree == base_tree
            and (
                not expectation.get("source_base_commit")
                or verified_commit == expectation.get("source_base_commit")
            )
            and (
                not expectation.get("source_base_tree")
                or verified_tree == expectation.get("source_base_tree")
            )
        )
    return bool(
        source_projection_valid(source_projection, expectation)
        and sha256_equal(
            report.get("source_projection_sha256"),
            source_projection_sha256(source_projection),
        )
        and (not base_commit or verified_commit == base_commit)
        and (not base_tree or verified_tree == base_tree)
    )


def candidate_rejection_is_conclusive(report: object) -> bool:
    """Return whether a bound projection rejection proves candidate failure."""
    return bool(
        isinstance(report, dict)
        and report.get("phase") == "source"
        and not report.get("expected_candidate_tree")
    )


def _projection_failure(
    expectation: dict[str, Any],
    *,
    phase: str,
    base_commit: str,
    base_tree: str,
    source_digest: str = "",
) -> dict[str, Any]:
    return {
        "schema": PROJECTION_FAILURE_SCHEMA,
        "status": "failed",
        "error_kind": "patch_not_applicable",
        "phase": phase,
        **{key: value for key, value in expectation.items() if key != "schema"},
        "verified_base_commit": base_commit,
        "verified_base_tree": base_tree,
        "source_projection_sha256": source_digest,
    }


def _expectation_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _EXPECTATION_KEYS:
        return False
    optional_oids = tuple(str(value.get(key) or "") for key in _IDENTITY_KEYS[5:])
    return bool(
        value.get("schema") == EXPECTATION_SCHEMA
        and all(isinstance(value.get(key), str) for key in _IDENTITY_KEYS)
        and value.get("instance_id")
        and value.get("record_id")
        and all(canonical_sha256(value.get(key)) is not None for key in _SHA256_IDENTITY_KEYS)
        and all(not oid or _OID_RE.fullmatch(oid) for oid in optional_oids)
        and len({bool(value.get(key)) for key in (
            "source_base_commit", "source_anonymous_base", "source_base_tree"
        )}) == 1
    )


def source_projection_sha256(value: dict[str, Any]) -> str:
    """Return the canonical digest stored by a prepared projection."""
    payload = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def source_projection_valid(report: object, expectation: object) -> bool:
    """Validate the source-tree projection created before public preparation."""
    if not isinstance(report, dict) or not _expectation_valid(expectation):
        return False
    source_base = str(report.get("verified_source_base_tree") or "")
    source_candidate = str(report.get("verified_source_candidate_tree") or "")
    source_commit = str(report.get("verified_source_base_commit") or "")
    source_anonymous = str(report.get("verified_source_anonymous_base") or "")
    expected_tree = str(expectation.get("expected_candidate_tree") or "")
    return bool(
        set(report) == _SOURCE_KEYS
        and report.get("schema") == SOURCE_PROJECTION_SCHEMA
        and report.get("status") == "verified"
        and _identity_values_match(report, expectation)
        and all(_OID_RE.fullmatch(oid) for oid in (
            source_commit, source_anonymous, source_base, source_candidate
        ))
        and len({len(source_commit), len(source_anonymous), len(source_base), len(source_candidate)}) == 1
        and source_anonymous == anonymous_commit_oid(source_base)
        and source_candidate != source_base
        and all(
            not expectation.get(key) or report.get(verified) == expectation.get(key)
            for key, verified in (
                ("source_base_commit", "verified_source_base_commit"),
                ("source_anonymous_base", "verified_source_anonymous_base"),
                ("source_base_tree", "verified_source_base_tree"),
            )
        )
        and (
            report.get("generation_tree_matches") is True and source_candidate == expected_tree
            if expected_tree
            else report.get("generation_tree_matches") is None
        )
    )


def candidate_projection_valid(
    report: object,
    expectation: object,
    source_projection: object | None = None,
) -> bool:
    """Validate a complete v1 or v2 projection without external base evidence."""
    if not isinstance(report, dict) or not _expectation_valid(expectation):
        return False
    if report.get("status") != "verified" or not _identity_values_match(
        report, expectation
    ):
        return False
    expected_tree = str(expectation.get("expected_candidate_tree") or "")
    if report.get("schema") == "opencollab.eval_candidate_projection.v1":
        base = str(report.get("base_tree") or "")
        candidate = str(report.get("candidate_tree") or "")
        return bool(
            set(report) == _V1_KEYS
            and _OID_RE.fullmatch(str(report.get("base_commit") or ""))
            and _OID_RE.fullmatch(base)
            and _OID_RE.fullmatch(candidate)
            and len(candidate) == len(base)
            and candidate != base
            and (
                not report.get("source_anonymous_base")
                or report.get("source_anonymous_base") == report.get("base_commit")
            )
            and (
                not report.get("source_base_tree")
                or report.get("source_base_tree") == report.get("base_tree")
            )
            and (
                report.get("generation_tree_matches") is True and candidate == expected_tree
                if expected_tree
                else report.get("generation_tree_matches") is None
            )
        )
    if report.get("schema") != PROJECTION_SCHEMA:
        return False
    if not source_projection_valid(source_projection, expectation):
        return False
    source_base = str(report.get("verified_source_base_tree") or "")
    source_candidate = str(report.get("verified_source_candidate_tree") or "")
    prepared_base = str(report.get("prepared_base_tree") or "")
    prepared_candidate = str(report.get("prepared_candidate_tree") or "")
    return bool(
        set(report) == _V2_KEYS
        and sha256_equal(
            report.get("source_projection_sha256"),
            source_projection_sha256(source_projection),
        )
        and all(_OID_RE.fullmatch(str(report.get(key) or "")) for key in (
            "verified_source_base_commit", "verified_source_anonymous_base",
            "verified_source_base_tree", "verified_source_candidate_tree", "prepared_base_commit",
            "prepared_base_tree", "prepared_candidate_tree", "worktree_candidate_tree",
        ))
        and len(source_candidate) == len(source_base)
        and len(prepared_candidate) == len(prepared_base)
        and source_candidate != source_base
        and prepared_candidate != prepared_base
        and report.get("worktree_candidate_tree") == prepared_candidate
        and report.get("official_worktree_matches") is True
        and (
            not expectation.get("source_base_commit")
            or report.get("verified_source_base_commit") == expectation.get("source_base_commit")
        )
        and (
            not expectation.get("source_anonymous_base")
            or report.get("verified_source_anonymous_base")
            == expectation.get("source_anonymous_base")
        )
        and (
            not expectation.get("source_base_tree")
            or report.get("verified_source_base_tree") == expectation.get("source_base_tree")
        )
        and (
            report.get("generation_tree_matches") is True and source_candidate == expected_tree
            if expected_tree
            else report.get("generation_tree_matches") is None
        )
        and all(
            report.get(target) == source_projection.get(source)
            for target, source in (
                ("verified_source_base_commit", "verified_source_base_commit"),
                ("verified_source_anonymous_base", "verified_source_anonymous_base"),
                ("verified_source_base_tree", "verified_source_base_tree"),
                ("verified_source_candidate_tree", "verified_source_candidate_tree"),
                ("generation_tree_matches", "generation_tree_matches"),
            )
        )
    )


def prepared_candidate_projection_valid(
    report: object,
    expectation: object,
    source_projection: object,
) -> bool:
    """Validate a candidate whose trusted projection finished before apply."""
    if (
        not isinstance(report, dict)
        or not _expectation_valid(expectation)
        or not source_projection_valid(source_projection, expectation)
        or set(report) != _V2_KEYS
        or report.get("schema") != PROJECTION_SCHEMA
        or report.get("status") != "prepared"
        or not _identity_values_match(report, expectation)
        or not sha256_equal(
            report.get("source_projection_sha256"),
            source_projection_sha256(source_projection),
        )
        or report.get("worktree_candidate_tree") != ""
        or report.get("official_worktree_matches") is not None
    ):
        return False
    source_base = str(report.get("verified_source_base_tree") or "")
    source_candidate = str(report.get("verified_source_candidate_tree") or "")
    prepared_base = str(report.get("prepared_base_tree") or "")
    prepared_candidate = str(report.get("prepared_candidate_tree") or "")
    return bool(
        all(_OID_RE.fullmatch(str(report.get(key) or "")) for key in (
            "verified_source_base_commit", "verified_source_anonymous_base", "verified_source_base_tree",
            "verified_source_candidate_tree", "prepared_base_commit", "prepared_base_tree",
            "prepared_candidate_tree",
        ))
        and len(source_candidate) == len(source_base)
        and len(prepared_candidate) == len(prepared_base)
        and source_candidate != source_base
        and prepared_candidate != prepared_base
        and all(
            report.get(target) == source_projection.get(source)
            for target, source in (
                ("verified_source_base_commit", "verified_source_base_commit"),
                ("verified_source_anonymous_base", "verified_source_anonymous_base"),
                ("verified_source_base_tree", "verified_source_base_tree"),
                ("verified_source_candidate_tree", "verified_source_candidate_tree"),
                ("generation_tree_matches", "generation_tree_matches"),
            )
        )
    )


def _read_expectation(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateProjectionError("candidate expectation is unreadable") from exc
    if not isinstance(value, dict) or set(value) != _EXPECTATION_KEYS:
        raise CandidateProjectionError("candidate expectation has an invalid shape")
    if not _expectation_valid(value):
        raise CandidateProjectionError("candidate expectation contains an invalid identity")
    return value


def _git(
    repo: Path,
    args: list[str],
    env: dict[str, str],
    *,
    failure_kind: str = "projection_runtime_error",
    failure_context: dict[str, str] | None = None,
) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(repo),
                "-c",
                f"safe.directory={repo}",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateProjectionError("candidate projection Git command failed") from exc
    if completed.returncode != 0:
        if failure_kind == "patch_not_applicable":
            raise CandidateProjectionError(
                "candidate patch does not apply to the verified base",
                error_kind=failure_kind,
                context=failure_context,
            )
        detail = completed.stderr.strip().splitlines()[-1:] or ["unknown Git failure"]
        raise CandidateProjectionError(f"candidate projection Git command failed: {detail[0]}")
    return completed.stdout.strip()


def _git_bytes(
    repo: Path,
    args: list[str],
    env: dict[str, str],
    *,
    input_bytes: bytes | None = None,
) -> bytes:
    try:
        completed = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(repo),
                "-c",
                f"safe.directory={repo}",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env=env,
            input=input_bytes,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateProjectionError("candidate projection Git command failed") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip().splitlines()[-1:]
        raise CandidateProjectionError(
            f"candidate projection Git command failed: {(detail or ['unknown Git failure'])[0]}"
        )
    return completed.stdout


def project_candidate(repo: Path, base_commit: str, patch_path: Path) -> tuple[str, str, str]:
    """Return the resolved base commit, base tree, and candidate tree."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    base_commit = _git(repo, ["rev-parse", f"{base_commit}^{{commit}}"], environment)
    base_tree = _git(repo, ["rev-parse", f"{base_commit}^{{tree}}"], environment)
    with tempfile.TemporaryDirectory(prefix="opencollab-eval-index-") as temporary:
        environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        _git(repo, ["read-tree", base_tree], environment)
        _git(
            repo,
            ["apply", "--cached", "--binary", "--whitespace=nowarn", str(patch_path)],
            environment,
            failure_kind="patch_not_applicable",
            failure_context={"base_commit": base_commit, "base_tree": base_tree},
        )
        candidate_tree = _git(repo, ["write-tree"], environment)
    if _OID_RE.fullmatch(base_tree) is None or _OID_RE.fullmatch(candidate_tree) is None:
        raise CandidateProjectionError("candidate projection returned an invalid tree identity")
    return base_commit, base_tree, candidate_tree


def _patch_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CandidateProjectionError("evaluation patch is unreadable") from exc


def build_source_projection(
    repo: Path,
    base_commit: str,
    patch_path: Path,
    expectation_path: Path,
    declared_base_commit: str | None = None,
) -> dict[str, Any]:
    """Verify the candidate against the dataset source tree before public setup."""
    expectation = _read_expectation(expectation_path)
    patch_sha256 = _patch_sha256(patch_path)
    if not sha256_equal(patch_sha256, expectation["eval_patch_sha256"]):
        raise CandidateProjectionError("evaluation patch SHA-256 does not match its expectation")
    try:
        resolved_source_base, base_tree, candidate_tree = project_candidate(
            repo, base_commit, patch_path
        )
    except CandidateProjectionError as exc:
        if exc.error_kind == "patch_not_applicable":
            exc.context["failure_report"] = _projection_failure(
                expectation,
                phase="source",
                base_commit=exc.context["base_commit"],
                base_tree=exc.context["base_tree"],
            )
        raise
    declared_base = str(declared_base_commit or resolved_source_base).strip().lower()
    if _OID_RE.fullmatch(declared_base) is None:
        raise CandidateProjectionError("declared evaluation base commit is invalid")
    if expectation["source_base_commit"] and declared_base != expectation["source_base_commit"]:
        raise CandidateProjectionError("evaluation base commit differs from generation")
    if expectation["source_base_commit"] and resolved_source_base != expectation["source_base_commit"]:
        raise CandidateProjectionError("evaluation source base differs from generation")
    anonymous_base = anonymous_commit_oid(base_tree)
    if expectation["source_anonymous_base"] and anonymous_base != expectation["source_anonymous_base"]:
        raise CandidateProjectionError("evaluation anonymous base differs from generation")
    if expectation["source_base_tree"] and base_tree != expectation["source_base_tree"]:
        raise CandidateProjectionError("evaluation base tree differs from generation")
    expected_tree = expectation["expected_candidate_tree"]
    if expected_tree and candidate_tree != expected_tree:
        raise CandidateProjectionError("evaluation candidate tree differs from generation")
    return {
        "schema": SOURCE_PROJECTION_SCHEMA,
        "status": "verified",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "verified_source_base_commit": resolved_source_base,
        "verified_source_anonymous_base": anonymous_base,
        "verified_source_base_tree": base_tree,
        "verified_source_candidate_tree": candidate_tree,
        "generation_tree_matches": candidate_tree == expected_tree if expected_tree else None,
    }


def _read_source_projection(path: Path, expectation: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateProjectionError("source candidate projection is unreadable") from exc
    if not source_projection_valid(value, expectation):
        raise CandidateProjectionError("source candidate projection identity is invalid")
    value["projection_sha256"] = source_projection_sha256(value)
    return value


def build_prepared_projection(
    repo: Path,
    prepared_base_commit: str,
    patch_path: Path,
    expectation_path: Path,
    source_projection_path: Path,
) -> dict[str, Any]:
    """Project the verified patch onto the public-prepared official base."""
    expectation = _read_expectation(expectation_path)
    patch_sha256 = _patch_sha256(patch_path)
    if not sha256_equal(patch_sha256, expectation["eval_patch_sha256"]):
        raise CandidateProjectionError("evaluation patch SHA-256 does not match its expectation")
    source = _read_source_projection(source_projection_path, expectation)
    try:
        prepared_base, prepared_tree, prepared_candidate = project_candidate(
            repo, prepared_base_commit, patch_path
        )
    except CandidateProjectionError as exc:
        if exc.error_kind == "patch_not_applicable":
            exc.context["failure_report"] = _projection_failure(
                expectation,
                phase="prepared",
                base_commit=exc.context["base_commit"],
                base_tree=exc.context["base_tree"],
                source_digest=source["projection_sha256"],
            )
        raise
    return {
        "schema": PROJECTION_SCHEMA,
        "status": "prepared",
        **{key: value for key, value in expectation.items() if key != "schema"},
        "source_projection_sha256": source["projection_sha256"],
        "verified_source_base_commit": source["verified_source_base_commit"],
        "verified_source_anonymous_base": source["verified_source_anonymous_base"],
        "verified_source_base_tree": source["verified_source_base_tree"],
        "verified_source_candidate_tree": source["verified_source_candidate_tree"],
        "prepared_base_commit": prepared_base,
        "prepared_base_tree": prepared_tree,
        "prepared_candidate_tree": prepared_candidate,
        "worktree_candidate_tree": "",
        "generation_tree_matches": source["generation_tree_matches"],
        "official_worktree_matches": None,
    }


def _worktree_candidate_tree(repo: Path, base_tree: str, candidate_tree: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    changed = _git_bytes(
        repo,
        ["diff-tree", "--no-commit-id", "--raw", "--no-renames", "-r", "-z", base_tree,
         candidate_tree],
        environment,
    )
    with tempfile.TemporaryDirectory(prefix="opencollab-eval-worktree-index-") as temporary:
        environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        _git(repo, ["read-tree", base_tree], environment)
        records = changed.split(b"\0")
        index = 0
        while index < len(records) and records[index]:
            if index + 1 >= len(records):
                raise CandidateProjectionError("candidate worktree diff is malformed")
            fields = records[index].split()
            path = os.fsdecode(records[index + 1])
            pure = PurePosixPath(path)
            if (
                len(fields) != 5
                or not fields[0].startswith(b":")
                or pure.is_absolute()
                or not pure.parts
                or ".." in pure.parts
            ):
                raise CandidateProjectionError("candidate worktree diff is malformed")
            mode, expected_oid = fields[1].decode("ascii"), fields[3].decode("ascii")
            if mode == "000000":
                _git(repo, ["update-index", "--force-remove", "--", path], environment)
                index += 2
                continue
            target = repo / pure
            try:
                info = target.lstat()
            except OSError as exc:
                raise CandidateProjectionError("candidate worktree path is unreadable") from exc
            if mode == "160000" and stat.S_ISDIR(info.st_mode):
                oid = _git(target, ["rev-parse", "HEAD^{commit}"], environment)
            elif mode == "120000" and stat.S_ISLNK(info.st_mode):
                oid = _git_bytes(
                    repo, ["hash-object", "-w", "--stdin"], environment,
                    input_bytes=os.fsencode(os.readlink(target)),
                ).decode("ascii").strip()
            elif mode in {"100644", "100755"} and stat.S_ISREG(info.st_mode):
                actual_mode = "100755" if stat.S_IMODE(info.st_mode) & 0o111 else "100644"
                if actual_mode != mode:
                    raise CandidateProjectionError("candidate worktree mode differs from projection")
                oid = _git_bytes(
                    repo, ["hash-object", "-w", "--no-filters", "--", path], environment,
                ).decode("ascii").strip()
            else:
                raise CandidateProjectionError("candidate worktree type differs from projection")
            if oid != expected_oid:
                raise CandidateProjectionError("candidate worktree content differs from projection")
            _git(repo, ["update-index", "--add", "--cacheinfo", mode, oid, path], environment)
            index += 2
        return _git(repo, ["write-tree"], environment)


def verify_prepared_worktree(
    repo: Path,
    patch_path: Path,
    projection_path: Path,
) -> dict[str, Any]:
    """Bind the applied official worktree to the prepared candidate tree."""
    try:
        value = json.loads(projection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateProjectionError("prepared candidate projection is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != PROJECTION_SCHEMA
        or value.get("status") != "prepared"
        or not sha256_equal(
            _patch_sha256(patch_path), value.get("eval_patch_sha256")
        )
        or _OID_RE.fullmatch(str(value.get("prepared_base_tree") or "")) is None
        or _OID_RE.fullmatch(str(value.get("prepared_candidate_tree") or "")) is None
    ):
        raise CandidateProjectionError("prepared candidate projection identity is invalid")
    worktree_tree = _worktree_candidate_tree(
        repo, value["prepared_base_tree"], value["prepared_candidate_tree"]
    )
    if worktree_tree != value["prepared_candidate_tree"]:
        raise CandidateProjectionError("official worktree differs from the prepared candidate")
    value.update(
        status="verified",
        worktree_candidate_tree=worktree_tree,
        official_worktree_matches=True,
    )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("source")
    source.add_argument("--repo", type=Path, required=True)
    source.add_argument("--base-commit", required=True)
    source.add_argument("--declared-base-commit", required=True)
    source.add_argument("--patch", type=Path, required=True)
    source.add_argument("--expectation", type=Path, required=True)
    source.add_argument("--output", type=Path, required=True)
    source.add_argument("--failure-output", type=Path)
    prepared = commands.add_parser("prepared")
    prepared.add_argument("--repo", type=Path, required=True)
    prepared.add_argument("--base-commit", required=True)
    prepared.add_argument("--patch", type=Path, required=True)
    prepared.add_argument("--expectation", type=Path, required=True)
    prepared.add_argument("--source-projection", type=Path, required=True)
    prepared.add_argument("--output", type=Path, required=True)
    prepared.add_argument("--failure-output", type=Path)
    verify = commands.add_parser("verify-worktree")
    verify.add_argument("--repo", type=Path, required=True)
    verify.add_argument("--patch", type=Path, required=True)
    verify.add_argument("--projection", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "source":
            projection = build_source_projection(
                args.repo, args.base_commit, args.patch, args.expectation,
                args.declared_base_commit,
            )
            output = args.output
        elif args.command == "prepared":
            projection = build_prepared_projection(
                args.repo, args.base_commit, args.patch, args.expectation,
                args.source_projection,
            )
            output = args.output
        else:
            projection = verify_prepared_worktree(args.repo, args.patch, args.projection)
            output = args.projection
    except CandidateProjectionError as exc:
        failure_output = getattr(args, "failure_output", None)
        failure_report = exc.context.get("failure_report")
        if failure_output is not None and isinstance(failure_report, dict):
            failure_output.write_text(
                json.dumps(failure_report, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print(str(exc))
        return 1
    output.write_text(json.dumps(projection, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CandidateProjectionError",
    "EXPECTATION_SCHEMA",
    "PROJECTION_FAILURE_SCHEMA",
    "PROJECTION_SCHEMA",
    "SOURCE_PROJECTION_SCHEMA",
    "build_prepared_projection",
    "build_source_projection",
    "candidate_projection_valid",
    "candidate_projection_failure_valid",
    "candidate_rejection_is_conclusive",
    "project_candidate",
    "source_projection_sha256",
    "source_projection_valid",
    "verify_prepared_worktree",
]
