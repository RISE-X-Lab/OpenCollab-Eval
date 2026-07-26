"""Build a candidate patch from a controller-owned Git baseline and worktree."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from opencollab_eval.patch_paths import is_generated_runtime_artifact_path

from .candidate_gitlinks import projection_ignored_roots
from .candidate_patch_files import (
    CandidateConstructionError,
    flatten_hardlinks,
    flatten_nested,
    reject_outward_symlinks,
    special_files,
    validate_file,
)
from .candidate_patch_git import (
    clean_git_environment as _clean_git_environment,
)
from .candidate_patch_git import (
    git_command as _command,
)
from .candidate_patch_git import (
    git_output as _output,
)
from .candidate_patch_git import (
    run_git as _run,
)
from .candidate_patch_ignore import trusted_ignore_overlay
from .candidate_patch_models import CandidatePatch, GitlinkProjection

_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRUSTED_ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"


def _install_trusted_attributes(git_dir: Path) -> None:
    info = git_dir / "info"
    if os.path.lexists(info) and (info.is_symlink() or not info.is_dir()):
        raise CandidateConstructionError("trusted candidate Git info path is unsafe")
    info.mkdir(exist_ok=True)
    attributes = info / "attributes"
    if os.path.lexists(attributes) and (attributes.is_symlink() or not attributes.is_file()):
        raise CandidateConstructionError("trusted candidate attribute policy is unsafe")
    attributes.write_text(_TRUSTED_ATTRIBUTES, encoding="utf-8")


def _safe_path(raw: bytes) -> str:
    path = os.fsdecode(raw)
    pure = PurePosixPath(path.rstrip("/"))
    if not pure.parts or pure.is_absolute() or ".." in pure.parts or "\x00" in path:
        raise CandidateConstructionError("candidate path escaped the worktree")
    return pure.as_posix() + ("/" if path.endswith("/") else "")


def _records(output: bytes, limit: int) -> tuple[bytes, ...]:
    records = tuple(record for record in output.split(b"\0") if record)
    if len(records) > limit:
        raise CandidateConstructionError("trusted candidate census exceeded its entry limit")
    return records


def _is_harness_path(path: str) -> bool:
    return any(
        part in {".git", ".opencollab"} or part.startswith(".opencollab-retired-")
        for part in PurePosixPath(path.rstrip("/")).parts
    )


def _under_roots(path: str, roots: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path.rstrip("/")).parts
    return any(parts[: len(PurePosixPath(root).parts)] == PurePosixPath(root).parts for root in roots)


def _stage_exact_paths(
    git: str,
    common: list[str],
    worktree: Path,
    paths: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
) -> None:
    for path in paths:
        info = validate_file(worktree, path, byte_limit)
        if stat.S_ISLNK(info.st_mode):
            oid = _output(
                git,
                [*common, "hash-object", "-w", "--stdin"],
                env=env,
                timeout=timeout,
                limit=256,
                label="candidate symlink object",
                payload=os.fsencode(os.readlink(worktree / path)),
            ).decode("ascii").strip()
            mode = "120000"
        else:
            oid = _output(
                git,
                [*common, "hash-object", "-w", "--no-filters", str(worktree / path)],
                env=env,
                timeout=timeout,
                limit=256,
                label="candidate file object",
            ).decode("ascii").strip()
            mode = "100755" if stat.S_IMODE(info.st_mode) & 0o111 else "100644"
        if not _OID_RE.fullmatch(oid):
            raise CandidateConstructionError("candidate object identity is invalid")
        _run(
            [*common, "update-index", "--add", "--cacheinfo", mode, oid, path],
            env=env,
            timeout=timeout,
        )


def _stage_control_changes(
    git: str,
    common: list[str],
    worktree: Path,
    paths: tuple[str, ...],
    preserved_roots: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
) -> tuple[str, ...]:
    selected = tuple(
        path
        for path in paths
        if not _is_harness_path(path) and not _under_roots(path, preserved_roots)
    )
    for path in selected:
        if os.path.lexists(worktree / path):
            _stage_exact_paths(
                git, common, worktree, (path,), env=env, timeout=timeout, byte_limit=byte_limit
            )
        else:
            _run(
                [*common, "update-index", "--force-remove", "--", path],
                env=env,
                timeout=timeout,
            )
    return selected


def _tree_entries(output: bytes, entry_limit: int) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for record in _records(output, entry_limit):
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise CandidateConstructionError("trusted baseline tree contains a malformed entry")
        mode, kind, oid = (value.decode("ascii") for value in fields)
        path = _safe_path(raw_path)
        if not _OID_RE.fullmatch(oid) or (mode, kind) not in {
            ("100644", "blob"), ("100755", "blob"), ("120000", "blob"), ("160000", "commit")
        }:
            raise CandidateConstructionError("trusted baseline tree contains an unsupported entry")
        entries.append((mode, oid, path))
    return tuple(entries)


def _untracked(
    git: str,
    common: list[str],
    worktree: Path,
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    entry_limit: int,
    file_byte_limit: int,
    preserved_roots: tuple[str, ...],
    ignored_roots: tuple[str, ...],
    added_control_paths: tuple[str, ...],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[str, ...],
    int,
]:
    flattened: list[tuple[str, str]] = []
    for _attempt in range(32):
        output = _output(
            git, [*common, "ls-files", "--others", "--exclude-standard", "-z"],
            env=env, timeout=timeout, limit=byte_limit, label="untracked census",
        )
        paths = tuple(sorted({
            *(_safe_path(record) for record in _records(output, entry_limit)),
            *added_control_paths,
        }))
        ignored_output = _output(
            git,
            [*common, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            env=env, timeout=timeout, limit=byte_limit, label="ignored directory census",
        )
        changed, current = flatten_nested(paths, worktree)
        flattened.extend(current)
        if changed:
            continue
        ignored_paths = tuple(_safe_path(record) for record in _records(ignored_output, entry_limit))
        excluded = tuple(
            path
            for path in paths
            if _is_harness_path(path)
            or _under_roots(path, preserved_roots)
            or _under_roots(path, ignored_roots)
            or is_generated_runtime_artifact_path(path)
        )
        selected = tuple(path for path in paths if path not in excluded)
        immediate = tuple(path for path in selected if path not in added_control_paths)
        for path in immediate:
            if path.endswith("/"):
                raise CandidateConstructionError(f"untracked directory {path} could not be projected")
            validate_file(worktree, path, file_byte_limit)
        _reject_unignored_special_files(
            git, common, worktree,
            (
                *ignored_paths,
                *(root.rstrip("/") + "/" for root in preserved_roots),
                *ignored_roots,
            ),
            env=env, timeout=timeout, byte_limit=byte_limit, entry_limit=entry_limit,
        )
        flattened_hardlinks = flatten_hardlinks(worktree, immediate, file_byte_limit)
        if immediate:
            payload = b"\0".join(os.fsencode(path) for path in immediate) + b"\0"
            _run(
                [*common, "add", "--pathspec-from-file=-", "--pathspec-file-nul"],
                env=env, timeout=timeout, payload=payload,
            )
        return (
            selected, excluded, tuple(flattened), ignored_paths, flattened_hardlinks,
            len(output) + len(ignored_output),
        )
    raise CandidateConstructionError("nested repository flattening did not converge")


def _raw_diff(output: bytes, entry_limit: int) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    records = output.split(b"\0")
    paths: list[str] = []
    modes: list[tuple[str, str, str]] = []
    index = 0
    while index < len(records) and records[index]:
        if len(paths) >= entry_limit or index + 1 >= len(records):
            raise CandidateConstructionError("candidate raw diff exceeded its entry limit")
        fields = records[index].split()
        path = _safe_path(records[index + 1])
        index += 2
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise CandidateConstructionError("candidate raw diff is malformed")
        modes.append((path, fields[0][1:].decode("ascii"), fields[1].decode("ascii")))
        paths.append(path)
    return tuple(paths), tuple(modes)


def _reject_unignored_special_files(
    git: str,
    common: list[str],
    worktree: Path,
    ignored_paths: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    byte_limit: int,
    entry_limit: int,
) -> None:
    paths = special_files(worktree, ignored_paths, entry_limit)
    if not paths:
        return
    payload = b"\0".join(os.fsencode(path) for path in paths) + b"\0"
    if len(payload) > byte_limit:
        raise CandidateConstructionError("special-file census exceeded its byte limit")
    ignore_env = dict(env)
    ignore_env.pop("GIT_LITERAL_PATHSPECS", None)
    output = _output(
        git,
        [*common, "check-ignore", "--no-index", "--stdin", "-z"],
        env=ignore_env,
        timeout=timeout,
        limit=byte_limit,
        label="special-file ignore census",
        payload=payload,
        allowed_returncodes=(0, 1),
    )
    ignored_special = {_safe_path(record) for record in _records(output, entry_limit)}
    unignored = tuple(path for path in paths if path not in ignored_special)
    if unignored:
        raise CandidateConstructionError(
            f"candidate {unignored[0]} is not representable in a Git patch"
        )


def _gitlink_actions(
    worktree: Path,
    entries: tuple[tuple[str, str, str], ...],
    projections: tuple[GitlinkProjection, ...],
) -> dict[str, GitlinkProjection]:
    baseline = {path: oid for mode, oid, path in entries if mode == "160000"}
    supplied = {item.path: item for item in projections}
    projection_ignored_roots(projections)
    if len(supplied) != len(projections):
        raise CandidateConstructionError("candidate Gitlink projection path is duplicated")
    if set(supplied) != set(baseline):
        raise CandidateConstructionError("every trusted baseline Gitlink requires one explicit projection")
    for path, item in supplied.items():
        if item.oid != baseline[path] or item.action not in {"preserve", "delete", "replacement"}:
            raise CandidateConstructionError("candidate Gitlink projection is invalid")
        if any(value is not None and not _SHA256_RE.fullmatch(value) for value in (
            item.baseline_digest,
            item.current_digest,
        )):
            raise CandidateConstructionError("candidate Gitlink digest is invalid")
        exists = os.path.lexists(worktree / path)
        if item.action == "preserve":
            if item.baseline_digest != item.current_digest:
                raise CandidateConstructionError("materialized Gitlink content changed without projection")
            if exists and not (worktree / path).is_dir():
                raise CandidateConstructionError("preserved Gitlink became an ordinary file")
        elif item.action == "delete" and exists:
            raise CandidateConstructionError("deleted Gitlink remains visible in the worktree")
        elif item.action == "replacement" and not exists:
            raise CandidateConstructionError("replacement Gitlink has no visible content")
    return supplied


def construct_candidate_patch(
    *,
    git_dir: Path,
    worktree: Path,
    base: str,
    baseline_sha256: str,
    max_patch_bytes: int,
    max_file_bytes: int = 64 * 1024 * 1024,
    max_census_bytes: int = 64 * 1024 * 1024,
    max_census_entries: int = 1_000_000,
    timeout: float = 120,
    gitlinks: tuple[GitlinkProjection, ...] = (),
) -> CandidatePatch:
    """Construct one canonical patch without consulting Solver-owned Git metadata."""
    git = shutil.which("git")
    if not git or not git_dir.is_dir() or git_dir.is_symlink() or not worktree.is_dir():
        raise CandidateConstructionError("trusted candidate baseline or worktree is unavailable")
    if not _OID_RE.fullmatch(base) or not _SHA256_RE.fullmatch(baseline_sha256):
        raise CandidateConstructionError("trusted candidate identity is invalid")
    if min(max_patch_bytes, max_file_bytes, max_census_bytes, max_census_entries) <= 0:
        raise CandidateConstructionError("trusted candidate limits are invalid")
    with tempfile.TemporaryDirectory(prefix="opencollab-candidate-") as temporary:
        index = Path(temporary) / "index"
        objects = Path(temporary) / "objects"
        objects.mkdir()
        _install_trusted_attributes(git_dir)
        env = _clean_git_environment(index, objects, git_dir)
        common = _command(git, git_dir, worktree)
        tree_output = _output(
            git, [*common, "ls-tree", "-rz", "--full-tree", base], env=env,
            timeout=timeout, limit=max_census_bytes, label="baseline tree census",
        )
        entries = _tree_entries(tree_output, max_census_entries)
        base_tree = _output(
            git, [*common, "rev-parse", f"{base}^{{tree}}"], env=env,
            timeout=timeout, limit=256, label="base tree identity",
        ).decode("ascii").strip()
        if not _OID_RE.fullmatch(base_tree):
            raise CandidateConstructionError("trusted base tree identity is invalid")
        projections = _gitlink_actions(worktree, entries, gitlinks)
        gitlink_ignored_roots = projection_ignored_roots(gitlinks)
        preserved_roots = tuple(
            path for path, projection in projections.items() if projection.action == "preserve"
        )
        tracked = tuple((oid, path) for mode, oid, path in entries if mode != "160000")
        for oid, path in tracked:
            target = worktree / path
            if not os.path.lexists(target) or target.is_dir() or target.is_symlink():
                continue
            info = target.lstat()
            if info.st_size <= max_file_bytes:
                continue
            baseline_size = _output(
                git,
                [*common, "cat-file", "-s", oid],
                env=env,
                timeout=timeout,
                limit=64,
                label="baseline blob size",
            ).decode("ascii").strip()
            if not baseline_size.isdigit() or info.st_size != int(baseline_size):
                raise CandidateConstructionError(f"candidate {path} exceeded its file byte limit")
        _run([*common, "read-tree", base], env=env, timeout=timeout)
        for path, projection in projections.items():
            if projection.action != "preserve":
                _run([*common, "update-index", "--force-remove", "--", path], env=env, timeout=timeout)
        with trusted_ignore_overlay(
            git, git_dir, worktree, entries, env=env, timeout=timeout,
            byte_limit=max_census_bytes, entry_limit=max_census_entries,
            control_names=(".gitignore",),
        ) as control_state:
            _run([*common, "add", "-u"], env=env, timeout=timeout)
            tracked_raw = _output(
                git,
                [*common, "diff", "--cached", "--raw", "-z", "--no-renames", base, "--"],
                env=env, timeout=timeout, limit=max_census_bytes,
                label="changed tracked path census",
            )
            tracked_paths, tracked_modes = _raw_diff(tracked_raw, max_census_entries)
            for path, _old, new in tracked_modes:
                if new != "000000" and os.path.lexists(worktree / path):
                    validate_file(worktree, path, max_file_bytes)
            flattened_hardlinks = list(flatten_hardlinks(worktree, tracked_paths, max_file_bytes))
            (
                selected, excluded, flattened_repositories, _ignored_paths,
                untracked_hardlinks, untracked_bytes,
            ) = _untracked(
                git, common, worktree, env=env, timeout=timeout,
                byte_limit=max_census_bytes, entry_limit=max_census_entries,
                file_byte_limit=max_file_bytes, preserved_roots=preserved_roots,
                ignored_roots=gitlink_ignored_roots,
                added_control_paths=control_state.added_paths,
            )
            changed_controls = control_state.changed_paths
        control_paths = tuple(
            path
            for path in changed_controls
            if not _is_harness_path(path) and not _under_roots(path, preserved_roots)
        )
        flattened_hardlinks.extend(flatten_hardlinks(worktree, control_paths, max_file_bytes))
        _stage_control_changes(
            git, common, worktree, control_paths, preserved_roots,
            env=env, timeout=timeout, byte_limit=max_file_bytes,
        )
        for path in selected:
            validate_file(worktree, path, max_file_bytes)
        flattened_hardlinks.extend(untracked_hardlinks)
        for path, projection in projections.items():
            if projection.action == "preserve":
                _run(
                    [*common, "update-index", "--add", "--cacheinfo", "160000", projection.oid, path],
                    env=env, timeout=timeout,
                )
        candidate_tree = _output(
            git, [*common, "write-tree"], env=env, timeout=timeout,
            limit=256, label="candidate tree identity",
        ).decode("ascii").strip()
        raw = _output(
            git, [*common, "diff", "--cached", "--raw", "-z", "--no-renames", base, "--"],
            env=env, timeout=timeout, limit=max_census_bytes, label="candidate path census",
        )
        paths, modes = _raw_diff(raw, max_census_entries)
        if any(_is_harness_path(path) for path in paths):
            raise CandidateConstructionError("candidate contains a harness-owned path")
        reject_outward_symlinks(worktree, paths)
        for path, _old, new in modes:
            if new != "000000" and os.path.lexists(worktree / path):
                validate_file(worktree, path, max_file_bytes)
        patch_bytes = _output(
            git,
            [*common, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
             "--no-textconv", "--no-renames", base, "--"],
            env=env, timeout=timeout, limit=max_patch_bytes, label="candidate patch",
        )
    try:
        patch = patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CandidateConstructionError("candidate patch is not UTF-8") from exc
    if not _OID_RE.fullmatch(candidate_tree):
        raise CandidateConstructionError("candidate tree identity is invalid")
    status = "".join(
        f"{'A' if old == '000000' else 'D' if new == '000000' else 'M'}  {path}\n"
        for path, old, new in modes
    )
    return CandidatePatch(
        patch=patch,
        patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        anonymous_base=base,
        base_tree=base_tree,
        baseline_sha256=baseline_sha256,
        candidate_tree=candidate_tree,
        changed_paths=paths,
        path_modes=modes,
        untracked_paths=selected,
        excluded_harness_paths=excluded,
        flattened_repositories=flattened_repositories,
        flattened_hardlinks=tuple(flattened_hardlinks),
        census_bytes=len(tree_output) + untracked_bytes + len(raw),
        census_entries=len(entries) + len(selected) + len(paths),
        status=status,
    )
