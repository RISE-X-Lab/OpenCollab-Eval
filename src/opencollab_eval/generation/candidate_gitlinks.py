"""Controller-owned Gitlink baseline capture and visible-state projection."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .candidate_patch_files import CandidateConstructionError
from .candidate_patch_ignore import trusted_ignore_entries, trusted_ignore_view
from .candidate_patch_models import GitlinkProjection
from .gen_prediction_patch_git import (
    bounded_git_output,
    gitlink_repository_digest,
    prepare_gitlink_state_repositories,
)

_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_GITLINK_MANIFEST_BYTES = 1024 * 1024
MAX_GITLINK_CENSUS_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Snapshot:
    removed_gitlinks: tuple[tuple[str, str], ...]


def _environment(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "xdg"),
        GIT_ATTR_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_LITERAL_PATHSPECS="1",
        GIT_NO_REPLACE_OBJECTS="1",
        LC_ALL="C",
    )
    return env


def _gitlinks(git: str, git_dir: Path, worktree: Path, base: str, env: dict[str, str]) -> tuple[tuple[str, str], ...]:
    output = bounded_git_output(
        git,
        [f"--git-dir={git_dir}", f"--work-tree={worktree}", "ls-tree", "-rz", "--full-tree", base],
        env=env,
        timeout=120,
        max_bytes=MAX_GITLINK_CENSUS_BYTES,
        label="Gitlink baseline census",
    )
    items: list[tuple[str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise CandidateConstructionError("Gitlink baseline census is malformed")
        if fields[0] != b"160000":
            continue
        path = PurePosixPath(os.fsdecode(raw_path))
        oid = fields[2].decode("ascii")
        if path.is_absolute() or not path.parts or ".." in path.parts or not _OID_RE.fullmatch(oid):
            raise CandidateConstructionError("Gitlink baseline identity is invalid")
        items.append((path.as_posix(), oid))
    if len(items) > 10_000:
        raise CandidateConstructionError("Gitlink baseline census exceeded its entry limit")
    return tuple(items)


def visible_unmaterialized_gitlinks(
    *,
    git: str,
    git_dir: Path,
    worktree: Path,
    base: str,
    paths: tuple[str, ...],
    env: dict[str, str],
    timeout: float,
) -> frozenset[str]:
    """Find Gitlink paths with candidate-visible content under normal ignore rules."""
    if not paths:
        return frozenset()
    ignore_entries = trusted_ignore_entries(
        git,
        git_dir,
        base,
        env=env,
        timeout=timeout,
        byte_limit=MAX_GITLINK_CENSUS_BYTES,
    )
    with tempfile.TemporaryDirectory(prefix="opencollab-gitlink-index-") as temporary:
        isolated = dict(env)
        isolated["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        isolated["GIT_LITERAL_PATHSPECS"] = "1"
        def run(args: list[str], *, selected_worktree: Path = worktree) -> bytes:
            return bounded_git_output(
                git,
                [f"--git-dir={git_dir}", f"--work-tree={selected_worktree}", *args],
                env=isolated,
                timeout=timeout,
                max_bytes=MAX_GITLINK_CENSUS_BYTES,
                label="Gitlink visibility census",
            )

        run(["read-tree", base])
        for path in paths:
            run(["update-index", "--force-remove", "--", path])
        with trusted_ignore_view(
            git,
            git_dir,
            worktree,
            ignore_entries,
            env=isolated,
            timeout=timeout,
            byte_limit=MAX_GITLINK_CENSUS_BYTES,
            entry_limit=1_000_000,
        ) as ignore_state:
            output = run(
                ["ls-files", "--others", "-z"],
                selected_worktree=ignore_state.worktree,
            )
    visible: set[str] = set()
    expected = set(paths)
    records = [raw for raw in output.split(b"\0") if raw]
    for raw in records:
        if not raw:
            continue
        candidate = os.fsdecode(raw).rstrip("/")
        parts = PurePosixPath(candidate).parts
        for count in range(1, len(parts) + 1):
            prefix = PurePosixPath(*parts[:count]).as_posix()
            if prefix in expected:
                visible.add(prefix)
    return frozenset(visible)


def _materialized_state(
    worktree: Path,
    git_dir: Path,
    git: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> tuple[bool, tuple[str, ...]]:
    base = bounded_git_output(
        git,
        [f"--git-dir={git_dir}", "rev-parse", "HEAD"],
        env=env,
        timeout=timeout,
        max_bytes=256,
        label="materialized Gitlink base identity",
    ).decode("ascii").strip()
    entries = trusted_ignore_entries(
        git,
        git_dir,
        base,
        env=env,
        timeout=timeout,
        byte_limit=MAX_GITLINK_CENSUS_BYTES,
    )
    status = bounded_git_output(
        git,
        [
            f"--git-dir={git_dir}",
            f"--work-tree={worktree}",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ],
        env=env,
        timeout=timeout,
        max_bytes=MAX_GITLINK_CENSUS_BYTES,
        label="materialized Gitlink tracked status",
    )
    with trusted_ignore_view(
        git,
        git_dir,
        worktree,
        entries,
        env=env,
        timeout=timeout,
        byte_limit=MAX_GITLINK_CENSUS_BYTES,
        entry_limit=1_000_000,
    ) as ignore_state:
        untracked = bounded_git_output(
            git,
            [
                f"--git-dir={git_dir}",
                f"--work-tree={ignore_state.worktree}",
                "ls-files",
                "--others",
                "-z",
            ],
            env=env,
            timeout=timeout,
            max_bytes=MAX_GITLINK_CENSUS_BYTES,
            label="materialized Gitlink untracked census",
        )
    return bool(status.rstrip(b"\0") or untracked.rstrip(b"\0")), ignore_state.ignored_paths


def projection_ignored_roots(projections: tuple[GitlinkProjection, ...]) -> tuple[str, ...]:
    """Validate and return root-relative ignored paths carried by Gitlink projections."""
    roots: list[str] = []
    for projection in projections:
        base = PurePosixPath(projection.path)
        if projection.action == "delete" and projection.ignored_paths:
            raise CandidateConstructionError("deleted Gitlink cannot carry ignored paths")
        for path in projection.ignored_paths:
            pure = PurePosixPath(path.rstrip("/"))
            if (
                pure.is_absolute()
                or not pure.parts
                or ".." in pure.parts
                or "\x00" in path
                or pure.parts[: len(base.parts)] != base.parts
                or len(pure.parts) <= len(base.parts)
            ):
                raise CandidateConstructionError("candidate Gitlink ignored path is invalid")
            roots.append(pure.as_posix() + ("/" if path.endswith("/") else ""))
    return tuple(sorted(set(roots)))


def capture_gitlink_manifest(
    *,
    git_dir: Path,
    worktree: Path,
    base: str,
    base_tree: str,
    baseline_sha256: str,
    repository_directory: Path,
) -> dict[str, object]:
    """Capture trusted visible Gitlink state before Solver execution."""
    if not _OID_RE.fullmatch(base) or not _OID_RE.fullmatch(base_tree) or not _SHA256_RE.fullmatch(baseline_sha256):
        raise CandidateConstructionError("Gitlink baseline binding is invalid")
    git = shutil.which("git")
    if not git:
        raise CandidateConstructionError("trusted candidate Git executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="opencollab-gitlink-home-") as temporary:
        home = Path(temporary)
        (home / "xdg").mkdir()
        env = _environment(home)
        entries = _gitlinks(git, git_dir, worktree, base, env)
        for path, _oid in entries:
            target = worktree / path
            if os.path.lexists(target) and (target.is_symlink() or not target.is_dir()):
                raise CandidateConstructionError("materialized Gitlink baseline is not a directory")
        snapshot = _Snapshot(entries)
        repositories = dict(
            prepare_gitlink_state_repositories(
                worktree, snapshot, repository_directory, git, env=env, timeout=120
            )
        )
        digests = {
            path: gitlink_repository_digest(repository, git, env=env, timeout=120)
            for path, repository in repositories.items()
        }
    return {
        "schema": "opencollab.candidate_gitlinks.v1",
        "anonymous_base": base,
        "base_tree": base_tree,
        "baseline_sha256": baseline_sha256,
        "gitlinks": [
            {
                "path": path,
                "oid": oid,
                "baseline_digest": digests.get(path),
                "baseline_repository": repositories.get(path).name if path in repositories else None,
            }
            for path, oid in entries
        ],
    }


def derive_gitlink_projections(
    *,
    worktree: Path,
    entries: tuple[tuple[str, str, str | None, Path | None], ...],
    git: str,
    env: dict[str, str],
    timeout: float,
    visible_unmaterialized: frozenset[str] = frozenset(),
) -> tuple[GitlinkProjection, ...]:
    """Derive candidate actions from trusted baseline state and visible files."""
    projections: list[GitlinkProjection] = []
    for path, oid, expected, baseline_repository in entries:
        target = worktree / path
        current: str | None = None
        ignored: tuple[str, ...] = ()
        if not os.path.lexists(target):
            action = "delete" if expected is not None else "preserve"
        elif target.is_symlink() or not target.is_dir():
            action = "replacement"
        elif expected is None:
            action = "replacement" if path in visible_unmaterialized else "preserve"
        else:
            changed, nested_ignored = _materialized_state(
                target,
                baseline_repository,
                git,
                env=env,
                timeout=timeout,
            )
            ignored = tuple(
                f"{path}/{item}" for item in nested_ignored
            )
            if changed:
                action = "replacement"
            else:
                action = "preserve"
                current = expected
        projections.append(GitlinkProjection(path, oid, action, expected, current, ignored))
    return tuple(projections)


def project_gitlink_manifest(
    *,
    manifest: dict[str, object],
    worktree: Path,
    git_dir: Path,
    repository_directory: Path,
) -> dict[str, object]:
    """Create a bounded projection manifest after Solver execution."""
    items = manifest.get("gitlinks")
    base = manifest.get("anonymous_base")
    base_tree = manifest.get("base_tree")
    baseline_sha256 = manifest.get("baseline_sha256")
    if (
        manifest.get("schema") != "opencollab.candidate_gitlinks.v1"
        or not isinstance(items, list)
        or len(items) > 10_000
        or not isinstance(base, str)
        or not _OID_RE.fullmatch(base)
        or not isinstance(base_tree, str)
        or not _OID_RE.fullmatch(base_tree)
        or not isinstance(baseline_sha256, str)
        or not _SHA256_RE.fullmatch(baseline_sha256)
    ):
        raise CandidateConstructionError("Gitlink baseline manifest is invalid")
    entries: list[tuple[str, str, str | None, Path | None]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path", "oid", "baseline_digest", "baseline_repository"
        }:
            raise CandidateConstructionError("Gitlink baseline entry is invalid")
        path, oid, digest, repository = (
            item.get("path"), item.get("oid"), item.get("baseline_digest"), item.get("baseline_repository")
        )
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if (
            pure is None
            or pure.is_absolute()
            or not pure.parts
            or ".." in pure.parts
            or path in seen
            or not isinstance(oid, str)
            or not _OID_RE.fullmatch(oid)
            or len(oid) != len(base)
            or (digest is not None and (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)))
            or (repository is not None and (not isinstance(repository, str) or Path(repository).name != repository))
        ):
            raise CandidateConstructionError("Gitlink baseline entry is invalid")
        seen.add(path)
        baseline_repository = repository_directory / repository if isinstance(repository, str) else None
        if baseline_repository is not None and (
            not baseline_repository.is_dir() or baseline_repository.is_symlink()
        ):
            raise CandidateConstructionError("Gitlink baseline repository is unavailable")
        entries.append(
            (path, oid, digest if isinstance(digest, str) else None, baseline_repository)
        )
    git = shutil.which("git")
    if not git:
        raise CandidateConstructionError("trusted candidate Git executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="opencollab-gitlink-home-") as temporary:
        home = Path(temporary)
        (home / "xdg").mkdir()
        env = _environment(home)
        visible = visible_unmaterialized_gitlinks(
            git=git,
            git_dir=git_dir,
            worktree=worktree,
            base=base,
            paths=tuple(path for path, _oid, digest, _repository in entries if digest is None),
            env=env,
            timeout=120,
        )
        projections = derive_gitlink_projections(
            worktree=worktree,
            entries=tuple(entries),
            git=git,
            env=env,
            timeout=120,
            visible_unmaterialized=visible,
        )
    return {
        "schema": "opencollab.candidate_gitlink_projections.v1",
        "anonymous_base": base,
        "base_tree": base_tree,
        "baseline_sha256": baseline_sha256,
        "gitlinks": [
            {
                "path": item.path,
                "oid": item.oid,
                "action": item.action,
                "baseline_digest": item.baseline_digest,
                "current_digest": item.current_digest,
                "ignored_paths": list(item.ignored_paths),
            }
            for item in projections
        ],
    }


def read_manifest(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    if len(payload) > MAX_GITLINK_MANIFEST_BYTES:
        raise CandidateConstructionError("Gitlink manifest exceeded its byte limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise CandidateConstructionError("Gitlink manifest is invalid")
    return value


def replay_gitlink_paths(manifest: dict[str, object]) -> tuple[str, ...]:
    """Return validated materialized paths that must be removed before patch replay."""
    items = manifest.get("gitlinks")
    if manifest.get("schema") != "opencollab.candidate_gitlink_projections.v1" or not isinstance(items, list):
        raise CandidateConstructionError("Gitlink projection manifest is invalid")
    replay: list[str] = []
    projections: list[GitlinkProjection] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path", "oid", "action", "baseline_digest", "current_digest", "ignored_paths"
        }:
            raise CandidateConstructionError("Gitlink projection entry is invalid")
        path, action, ignored = item.get("path"), item.get("action"), item.get("ignored_paths")
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if (
            pure is None
            or pure.is_absolute()
            or not pure.parts
            or ".." in pure.parts
            or path in seen
            or action not in {"preserve", "delete", "replacement"}
            or not isinstance(ignored, list)
            or any(not isinstance(value, str) for value in ignored)
        ):
            raise CandidateConstructionError("Gitlink projection entry is invalid")
        seen.add(path)
        projections.append(
            GitlinkProjection(
                path,
                str(item.get("oid") or ""),
                action,
                item.get("baseline_digest") if isinstance(item.get("baseline_digest"), str) else None,
                item.get("current_digest") if isinstance(item.get("current_digest"), str) else None,
                tuple(ignored),
            )
        )
        if action != "preserve":
            replay.append(path)
    projection_ignored_roots(tuple(projections))
    return tuple(replay)
