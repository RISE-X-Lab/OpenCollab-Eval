"""Build a disposable solver worktree from one verified Git commit."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

if __package__:
    from opencollab_eval.engine.workspace_integrity import (
        FailureScope,
        FindingOrigin,
        IntegrityAction,
        IntegrityPhase,
        WorkspaceChange,
        WorkspaceFinding,
        classify_finding,
        failure_report,
    )
    from opencollab_eval.generation import gen_prediction_snapshot_support as _snapshot_support
else:
    import opencollab_snapshot_support as _snapshot_support
    from opencollab_workspace_integrity import (
        FailureScope,
        FindingOrigin,
        IntegrityAction,
        IntegrityPhase,
        WorkspaceChange,
        WorkspaceFinding,
        classify_finding,
        failure_report,
    )

_anonymous_commit_oid = _snapshot_support.anonymous_commit_oid
_copy_public_preparation = _snapshot_support.copy_public_preparation
_replace_worktree_contents = _snapshot_support.replace_worktree_contents
_sanitize_preparation_repository = _snapshot_support.sanitize_preparation_repository
_workspace_sha256 = _snapshot_support.workspace_sha256

_OBJECT_ID_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_STATUS_PATH_SAMPLES = 32
_MAX_STATUS_SAMPLE_BYTES = 16 * 1024
_PREPARATION_INPUT_SCHEMA = "opencollab.preparation_input.v1"


class SnapshotSetupError(RuntimeError):
    """A task image cannot produce a verified disposable workspace."""

    def __init__(
        self,
        message: str,
        *,
        scope: FailureScope = FailureScope.IMAGE,
        report: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_scope = scope
        self.integrity_report = report or {}


def _clean_git_env(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": str(home),
            "LC_ALL": "C",
            "XDG_CONFIG_HOME": str(home / "xdg"),
        }
    )
    (home / "xdg").mkdir(parents=True, exist_ok=True)
    return env


def _discover_standard_repository(workspace: Path) -> tuple[Path, Path]:
    try:
        candidate = workspace.resolve(strict=True)
    except OSError as exc:
        raise SnapshotSetupError("solver workspace is missing") from exc
    if not candidate.is_dir():
        raise SnapshotSetupError("solver workspace is not a directory")
    while True:
        marker = candidate / ".git"
        if os.path.lexists(marker):
            if marker.is_symlink() or not marker.is_dir():
                raise SnapshotSetupError("solver repository metadata is not self-contained")
            return candidate, marker
        if candidate.parent == candidate:
            raise SnapshotSetupError("solver workspace is not inside a Git repository")
        candidate = candidate.parent


def _run_git(
    repo: Path,
    *args: str,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    safe_repo = str(repo.resolve(strict=True))
    result = subprocess.run(
        ["git", "-c", f"safe.directory={safe_repo}", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        env=env,
    )
    if len(result.stdout) > _MAX_GIT_OUTPUT_BYTES or len(result.stderr) > _MAX_GIT_OUTPUT_BYTES:
        raise SnapshotSetupError("Git snapshot command exceeded its output bound")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotSetupError(
            f"Git snapshot command failed ({args[0]}, exit {result.returncode}): {detail[:1000]}"
        )
    return result


def _git_text(repo: Path, *args: str, env: dict[str, str]) -> str:
    return _run_git(repo, *args, env=env).stdout.decode("utf-8", errors="strict").strip()


def _tree_entries(
    repo: Path,
    commit: str,
    *,
    env: dict[str, str],
) -> tuple[list[str], list[tuple[str, str]]]:
    tracked: list[str] = []
    gitlinks: list[tuple[str, str]] = []
    for record in _run_git(repo, "ls-tree", "-rz", "--full-tree", commit, env=env).stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise SnapshotSetupError("base tree contains a malformed entry")
        mode, kind, object_id = (field.decode("ascii", errors="strict") for field in fields)
        path = os.fsdecode(raw_path)
        _safe_relative(path)
        if _OBJECT_ID_RE.fullmatch(object_id) is None:
            raise SnapshotSetupError("base tree contains an invalid object id")
        if mode == "160000" and kind == "commit":
            gitlinks.append((path, object_id.lower()))
        elif kind == "blob" and mode in {"100644", "100755", "120000"}:
            tracked.append(path)
        else:
            raise SnapshotSetupError("base tree contains an unsupported tracked entry")
    return tracked, gitlinks


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\x00" in value:
        raise SnapshotSetupError("archive entry escaped the disposable workspace")
    return path


def _checkout_commit(
    repo: Path,
    commit: str,
    destination: Path,
    *,
    env: dict[str, str],
) -> None:
    source_git = Path(_git_text(repo, "rev-parse", "--absolute-git-dir", env=env))
    object_directory = source_git / "objects"
    if not object_directory.is_dir():
        raise SnapshotSetupError("source repository object directory is missing")
    temporary_git = Path(tempfile.mkdtemp(prefix="opencollab-index-", dir=destination.parent))
    try:
        _run_git(temporary_git, "init", "--bare", "-q", env=env)
        info = temporary_git / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text(
            "* -text -filter -ident -working-tree-encoding -export-ignore -export-subst\n",
            encoding="utf-8",
        )
        object_env = {**env, "GIT_OBJECT_DIRECTORY": str(object_directory)}
        _run_git(repo, f"--git-dir={temporary_git}", "read-tree", commit, env=object_env)
        destination.mkdir(parents=True, exist_ok=True)
        _run_git(
            repo,
            f"--git-dir={temporary_git}",
            f"--work-tree={destination}",
            "checkout-index",
            "--all",
            "--force",
            env=object_env,
        )
    finally:
        shutil.rmtree(temporary_git)


def _finding_record(
    finding: WorkspaceFinding,
    *,
    after: str,
) -> dict[str, object]:
    decision = classify_finding(finding)
    return {
        "observed_state": finding.as_dict(),
        "classification_basis": decision.basis,
        "action": decision.action.value,
        "verified_state_after_action": after,
        "failure_scope": decision.scope.value,
    }


def _reject_decision(report: dict[str, object], record: dict[str, object]) -> None:
    if record["action"] != IntegrityAction.TASK_FAILURE.value:
        return
    raise SnapshotSetupError(
        str(record["classification_basis"]),
        scope=FailureScope(str(record["failure_scope"])),
        report=report,
    )


def _initial_status(
    repo: Path,
    commit: str,
    *,
    env: dict[str, str],
) -> tuple[dict[str, object], dict[str, object], bool]:
    raw = _run_git(
        repo,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=all",
        env=env,
    ).stdout
    samples: dict[str, list[str]] = {"untracked": [], "ignored": []}
    counts = {"untracked": 0, "ignored": 0}
    sample_bytes = {"untracked": 0, "ignored": 0}
    for record in raw.split(b"\0"):
        kind = ""
        path = ""
        if record.startswith(b"? "):
            kind, path = "untracked", os.fsdecode(record[2:])
        elif record.startswith(b"! "):
            kind, path = "ignored", os.fsdecode(record[2:])
        if not kind:
            continue
        counts[kind] += 1
        encoded_bytes = len(path.encode("utf-8", errors="surrogateescape"))
        if (
            len(samples[kind]) < _MAX_STATUS_PATH_SAMPLES
            and sample_bytes[kind] + encoded_bytes <= _MAX_STATUS_SAMPLE_BYTES
        ):
            samples[kind].append(path)
            sample_bytes[kind] += encoded_bytes
    drift = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--quiet",
            "--ignore-submodules=all",
            commit,
            "--",
        ],
        env=env,
        check=False,
    ).returncode
    if drift not in {0, 1}:
        raise SnapshotSetupError("tracked baseline comparison failed")
    details = {
        kind: {
            "count": counts[kind],
            "sample": samples[kind],
            "truncated": counts[kind] > len(samples[kind]),
        }
        for kind in ("untracked", "ignored")
    }
    return details["untracked"], details["ignored"], drift == 1


def _outward_symlink_findings(
    root: Path,
    tracked: list[str],
    *,
    origin: FindingOrigin = FindingOrigin.BASE_COMMIT,
) -> list[WorkspaceFinding]:
    findings: list[WorkspaceFinding] = []
    for path in tracked:
        entry = root.joinpath(*PurePosixPath(path).parts)
        if not entry.is_symlink():
            continue
        resolved = entry.resolve(strict=False)
        try:
            resolved.relative_to(root)
            continue
        except ValueError:
            pass
        readable = resolved.exists()
        findings.append(
            WorkspaceFinding(
                kind="outward_symlink",
                phase=IntegrityPhase.BASELINE,
                origin=FindingOrigin.UNKNOWN if readable else origin,
                solver_readable=readable,
                removal_changes_semantics=readable,
                detail=path,
            )
        )
    return findings


def _materialize_gitlinks(
    source_root: Path,
    destination: Path,
    gitlinks: list[tuple[str, str]],
    *,
    env: dict[str, str],
) -> list[str]:
    materialized: list[str] = []
    for path, object_id in gitlinks:
        source = source_root.joinpath(*PurePosixPath(path).parts)
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink() or not source.is_dir():
            raise SnapshotSetupError("materialized Gitlink is not a directory")
        if not any(source.iterdir()):
            target = destination.joinpath(*PurePosixPath(path).parts)
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            continue
        target = destination.joinpath(*PurePosixPath(path).parts)
        target.mkdir(parents=True, exist_ok=True)
        try:
            _checkout_commit(source, object_id, target, env=env)
        except SnapshotSetupError as exc:
            raise SnapshotSetupError(
                "materialized Gitlink cannot reconstruct its verified baseline commit"
            ) from exc
        materialized.append(path)
    return materialized


def _initialize_anonymous_repository(
    root: Path, expected_tree: str | None, object_format: str,
    gitlinks: list[tuple[str, str]], *, env: dict[str, str],
) -> tuple[str, str]:
    init_args = ("init", "-q") if object_format == "sha1" else ("init", "-q", "--object-format=sha256")
    _run_git(root, *init_args, env=env)
    for key, value in (
        ("core.attributesFile", os.devnull),
        ("core.autocrlf", "false"),
        ("core.fsmonitor", "false"),
        ("core.hooksPath", os.devnull),
        ("diff.ignoreSubmodules", "all"),
    ):
        _run_git(root, "config", key, value, env=env)
    info = root / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* -text -filter -ident -working-tree-encoding\n", encoding="utf-8")
    (info / "exclude").write_text("/.opencollab/\n", encoding="utf-8")
    visible = _run_git(root, "ls-files", "--others", "-z", "--", ".", env=env).stdout
    if visible:
        _run_git(
            root, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
            env=env, input_bytes=visible,
        )
    for path, object_id in gitlinks:
        _run_git(root, "rm", "-q", "--cached", "-r", "--ignore-unmatch", "--", path, env=env)
        _run_git(root, "update-index", "--add", "--cacheinfo", f"160000,{object_id},{path}", env=env)
    base_tree = _git_text(root, "write-tree", env=env).lower()
    if expected_tree is not None and base_tree != expected_tree:
        raise SnapshotSetupError("disposable workspace tree differs from the verified base tree")
    commit_env = {
        **env,
        "GIT_AUTHOR_NAME": "OpenCollab Solver Snapshot",
        "GIT_AUTHOR_EMAIL": "solver-snapshot@invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_NAME": "OpenCollab Solver Snapshot",
        "GIT_COMMITTER_EMAIL": "solver-snapshot@invalid",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    anonymous_head = _run_git(
        root, "commit-tree", base_tree, env=commit_env, input_bytes=b"solver snapshot\n",
    ).stdout.decode("ascii").strip().lower()
    if anonymous_head != _anonymous_commit_oid(base_tree):
        raise SnapshotSetupError("anonymous repository identity differs from its base tree")
    _run_git(root, "update-ref", "HEAD", anonymous_head, env=env)
    for path, _object_id in gitlinks:
        worktree = root.joinpath(*PurePosixPath(path).parts)
        if worktree.is_dir() and not any(worktree.iterdir()):
            worktree.rmdir()
    return anonymous_head, base_tree


def _materialized_gitlink_evidence(
    root: Path,
    gitlinks: list[tuple[str, str]],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for path, object_id in gitlinks:
        entry = root.joinpath(*PurePosixPath(path).parts)
        if not entry.exists() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise SnapshotSetupError("materialized Gitlink is not a directory")
        evidence.append(
            {
                "path": path,
                "oid": object_id,
                "content_sha256": _workspace_sha256(entry),
            }
        )
    return evidence


def _index_gitlinks(repo: Path, *, env: dict[str, str]) -> list[tuple[str, str]]:
    gitlinks: list[tuple[str, str]] = []
    raw = _run_git(repo, "ls-files", "--stage", "-z", env=env).stdout
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise SnapshotSetupError("repository index contains a malformed entry")
        mode, object_id, stage = (field.decode("ascii", errors="strict") for field in fields)
        if stage != "0":
            raise SnapshotSetupError("public preparation left unresolved index entries")
        if mode != "160000":
            continue
        path = os.fsdecode(raw_path)
        _safe_relative(path)
        if _OBJECT_ID_RE.fullmatch(object_id) is None:
            raise SnapshotSetupError("repository index contains an invalid Gitlink object id")
        gitlinks.append((path, object_id.lower()))
    return gitlinks


def _replace_workspace(original: Path, prepared: Path) -> None:
    backup = original.with_name(f".{original.name}.opencollab-replaced-{os.getpid()}")
    if os.path.lexists(backup):
        raise SnapshotSetupError("disposable workspace replacement path already exists")
    try:
        original.rename(backup)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        for child in original.iterdir():
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        shutil.copytree(prepared, original, dirs_exist_ok=True, symlinks=True)
        shutil.rmtree(prepared)
        return
    try:
        prepared.rename(original)
    except BaseException:
        backup.rename(original)
        raise
    shutil.rmtree(backup)


def prepare_public_input(workspace: Path, expected_base_commit: str) -> dict[str, object]:
    """Restore a verified base while retaining object access for trusted public setup."""
    expected = str(expected_base_commit or "").strip().lower()
    if _OBJECT_ID_RE.fullmatch(expected) is None:
        raise SnapshotSetupError("expected base commit must be a full hexadecimal object id")
    object_format = "sha1" if len(expected) == 40 else "sha256"
    repo_root, _git_dir = _discover_standard_repository(workspace)
    temporary = Path(tempfile.mkdtemp(prefix=".opencollab-preparation-", dir=repo_root.parent))
    home = temporary.parent / f".{temporary.name}-home"
    home.mkdir(mode=0o700)
    report: dict[str, object] = {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [],
        "outcome": IntegrityAction.ALLOW.value,
        "failure_scope": FailureScope.NONE.value,
    }
    findings = report["findings"]
    assert isinstance(findings, list)
    try:
        env = _clean_git_env(home)
        if _git_text(repo_root, "rev-parse", f"{expected}^{{commit}}", env=env).lower() != expected:
            raise SnapshotSetupError("repository does not contain the expected base commit")
        base_tree = _git_text(repo_root, "rev-parse", f"{expected}^{{tree}}", env=env).lower()
        _tracked, gitlinks = _tree_entries(repo_root, expected, env=env)
        untracked, ignored, tracked_drift = _initial_status(repo_root, expected, env=env)
        if tracked_drift:
            findings.append(
                _finding_record(
                    WorkspaceFinding(
                        kind="tracked_content_drift",
                        phase=IntegrityPhase.BASELINE,
                        origin=FindingOrigin.UNKNOWN,
                        change=WorkspaceChange.MODIFIED,
                        solver_readable=True,
                        candidate_effect=True,
                        repairable=True,
                    ),
                    after="restored_from_verified_base",
                )
            )
        for kind, paths in (("untracked_content", untracked), ("ignored_content", ignored)):
            if paths["count"]:
                findings.append(
                    _finding_record(
                        WorkspaceFinding(
                            kind=kind,
                            phase=IntegrityPhase.BASELINE,
                            origin=FindingOrigin.UNKNOWN,
                            solver_readable=True,
                            repairable=True,
                            detail=json.dumps(paths, ensure_ascii=True, separators=(",", ":")),
                        ),
                        after="removed_from_disposable_task_copy",
                    )
                )
        _checkout_commit(repo_root, expected, temporary, env=env)
        _materialize_gitlinks(repo_root, temporary, gitlinks, env=env)
        materialized_gitlinks = _materialized_gitlink_evidence(temporary, gitlinks)
        workspace_sha256 = _workspace_sha256(temporary)
        _replace_worktree_contents(repo_root, temporary)
        _run_git(repo_root, "reset", "--mixed", "-q", expected, env=env)
        _sanitize_preparation_repository(repo_root, object_format)
        if _git_text(repo_root, "rev-parse", "HEAD", env=env).lower() != expected:
            raise SnapshotSetupError("public preparation input has the wrong base identity")
        if _git_text(
            repo_root,
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--ignore-submodules=all",
            env=env,
        ):
            raise SnapshotSetupError("public preparation input differs from the verified base")
        report["outcome"] = (
            IntegrityAction.SANITIZE.value if findings else IntegrityAction.ALLOW.value
        )
        return {
            "schema": _PREPARATION_INPUT_SCHEMA,
            "expected_base_commit": expected,
            "base_tree": base_tree,
            "workspace_sha256": workspace_sha256,
            "gitlinks": [{"path": path, "oid": oid} for path, oid in gitlinks],
            "materialized_gitlinks": materialized_gitlinks,
            "worktree_matches_base": True,
            "solver_started": False,
            "object_access_scope": "trusted_public_preparation_only",
            "workspace_integrity": report,
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if home.exists():
            shutil.rmtree(home)


def create_solver_snapshot(
    workspace: Path,
    expected_base_commit: str,
    *,
    filesystem_root: Path | None = None,
    baseline_drift_origin: FindingOrigin = FindingOrigin.UNKNOWN,
    preserve_workspace_state: bool = False,
) -> dict[str, object]:
    """Replace one task container's repository with a verified disposable copy."""
    del filesystem_root
    expected = str(expected_base_commit or "").strip().lower()
    if _OBJECT_ID_RE.fullmatch(expected) is None:
        raise SnapshotSetupError("expected base commit must be a full hexadecimal object id")
    object_format = "sha1" if len(expected) == 40 else "sha256"
    repo_root, _git_dir = _discover_standard_repository(workspace)
    temporary = Path(tempfile.mkdtemp(prefix=".opencollab-snapshot-", dir=repo_root.parent))
    report: dict[str, object] = {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [],
        "outcome": IntegrityAction.ALLOW.value,
        "failure_scope": FailureScope.NONE.value,
    }
    findings = report["findings"]
    assert isinstance(findings, list)
    try:
        home = temporary.parent / f".{temporary.name}-home"
        home.mkdir(mode=0o700)
        env = _clean_git_env(home)
        if _git_text(repo_root, "rev-parse", f"{expected}^{{commit}}", env=env).lower() != expected:
            raise SnapshotSetupError("repository does not contain the expected base commit")
        base_tree = _git_text(repo_root, "rev-parse", f"{expected}^{{tree}}", env=env).lower()
        tracked, gitlinks = _tree_entries(repo_root, expected, env=env)
        untracked, ignored, tracked_drift = _initial_status(repo_root, expected, env=env)
        if tracked_drift:
            trusted_preparation = preserve_workspace_state and baseline_drift_origin in {
                FindingOrigin.PUBLIC_INPUT,
                FindingOrigin.RUNTIME_DEPENDENCY,
            }
            record = _finding_record(
                WorkspaceFinding(
                    kind="tracked_content_drift",
                    phase=IntegrityPhase.BASELINE,
                    origin=baseline_drift_origin if trusted_preparation else FindingOrigin.UNKNOWN,
                    change=WorkspaceChange.MODIFIED,
                    solver_readable=True,
                    candidate_effect=True,
                    repairable=not preserve_workspace_state,
                    removal_changes_semantics=preserve_workspace_state,
                ),
                after=(
                    "recorded_as_public_preparation_baseline"
                    if trusted_preparation
                    else "restored_from_verified_base"
                    if not preserve_workspace_state
                    else "not_sanitized"
                ),
            )
            findings.append(record)
            if record["action"] == IntegrityAction.TASK_FAILURE.value:
                report.update(outcome=record["action"], failure_scope=record["failure_scope"])
            _reject_decision(report, record)
        for kind, paths in (("untracked_content", untracked), ("ignored_content", ignored)):
            if not paths["count"]:
                continue
            record = _finding_record(
                WorkspaceFinding(
                    kind=kind,
                    phase=IntegrityPhase.BASELINE,
                    origin=baseline_drift_origin if preserve_workspace_state else FindingOrigin.UNKNOWN,
                    change=WorkspaceChange.ADDED,
                    solver_readable=True,
                    repairable=not preserve_workspace_state,
                    removal_changes_semantics=preserve_workspace_state,
                    detail=json.dumps(paths, ensure_ascii=True, separators=(",", ":")),
                ),
                after=(
                    "recorded_as_public_preparation_baseline"
                    if preserve_workspace_state
                    else "excluded_from_disposable_copy"
                ),
            )
            findings.append(record)
        if preserve_workspace_state:
            current_gitlinks = _index_gitlinks(repo_root, env=env)
            tracked = _copy_public_preparation(repo_root, temporary)
            gitlinks = current_gitlinks
            materialized_gitlinks = _materialized_gitlink_evidence(temporary, gitlinks)
            materialized = [item["path"] for item in materialized_gitlinks]
            expected_tree = None
        else:
            _checkout_commit(repo_root, expected, temporary, env=env)
            materialized = _materialize_gitlinks(repo_root, temporary, gitlinks, env=env)
            materialized_gitlinks = _materialized_gitlink_evidence(temporary, gitlinks)
            expected_tree = base_tree
        for finding in _outward_symlink_findings(
            temporary,
            tracked,
            origin=baseline_drift_origin if preserve_workspace_state else FindingOrigin.BASE_COMMIT,
        ):
            record = _finding_record(finding, after="unchanged" if not finding.solver_readable else "blocked")
            findings.append(record)
            if record["action"] == IntegrityAction.TASK_FAILURE.value:
                report.update(outcome=record["action"], failure_scope=record["failure_scope"])
                _reject_decision(report, record)
        workspace_sha256 = _workspace_sha256(temporary)
        anonymous_head, base_tree = _initialize_anonymous_repository(
            temporary,
            expected_tree,
            object_format,
            gitlinks,
            env=env,
        )
        history_record = _finding_record(
            WorkspaceFinding(
                kind="repository_history_and_configuration",
                phase=IntegrityPhase.BASELINE,
                origin=FindingOrigin.UNKNOWN,
                solver_readable=True,
                repairable=True,
            ),
            after="one_anonymous_commit_no_remotes_or_replace_refs",
        )
        findings.append(history_record)
        _replace_workspace(repo_root, temporary)
        if _git_text(repo_root, "rev-list", "--all", "--count", env=env) != "1":
            raise SnapshotSetupError("disposable repository retained extra history", report=report)
        if _git_text(repo_root, "remote", env=env):
            raise SnapshotSetupError("disposable repository retained a remote", report=report)
        if _git_text(repo_root, "status", "--porcelain", "--untracked-files=no", env=env):
            raise SnapshotSetupError("disposable repository has tracked baseline changes", report=report)
        report["outcome"] = (
            IntegrityAction.SANITIZE.value
            if any(item["action"] == IntegrityAction.SANITIZE.value for item in findings)
            else IntegrityAction.ALLOW.value
        )
        return {
            "enabled": True,
            "anonymous_head": anonymous_head,
            "base_tree": base_tree,
            "workspace_sha256": workspace_sha256,
            "commit_count": 1,
            "remote_count": 0,
            "extra_git_metadata": 0,
            "removed_git_metadata": 1 + len(materialized),
            "removed_gitlinks": [
                {"path": path, "old_oid": object_id} for path, object_id in gitlinks
            ],
            "materialized_gitlinks": materialized_gitlinks,
            "expected_base_commit": expected,
            "workspace_integrity": report,
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        home_candidate = temporary.parent / f".{temporary.name}-home"
        if home_candidate.exists():
            shutil.rmtree(home_candidate)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    public_preparation = len(args) == 2 and args[0] == "--public-preparation"
    preparation_input = len(args) == 2 and args[0] == "--prepare-public-input"
    if len(args) != 1 and not public_preparation and not preparation_input:
        print(
            "usage: gen_prediction_snapshot_container.py [--prepare-public-input|--public-preparation] WORKSPACE",
            file=sys.stderr,
        )
        return 2
    try:
        encoded_base = sys.stdin.buffer.read(129)
        if len(encoded_base) > 128:
            raise SnapshotSetupError("expected base commit input exceeded its size bound")
        expected = encoded_base.decode("ascii", errors="strict")
        evidence = (
            prepare_public_input(Path(args[-1]), expected)
            if preparation_input
            else create_solver_snapshot(
                Path(args[-1]),
                expected,
                baseline_drift_origin=(
                    FindingOrigin.PUBLIC_INPUT
                    if public_preparation
                    else FindingOrigin.UNKNOWN
                ),
                preserve_workspace_state=public_preparation,
            )
        )
    except (OSError, SnapshotSetupError, UnicodeError, ValueError) as exc:
        report = getattr(exc, "integrity_report", {})
        if not report:
            report = failure_report(
                WorkspaceFinding(
                    kind="baseline_snapshot_failure",
                    phase=IntegrityPhase.BASELINE,
                    origin=FindingOrigin.UNKNOWN,
                    solver_readable=True,
                    removal_changes_semantics=True,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                verified_state_after_action="task_workspace_not_started",
            )
        scope = str(report.get("failure_scope") or FailureScope.IMAGE.value)
        print(json.dumps(
            {"enabled": False, "workspace_integrity": report}, ensure_ascii=True, sort_keys=True
        ))
        print(f"solver workspace snapshot failed [{scope}]: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
