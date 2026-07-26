"""Host-trusted, bounded extraction of a solver container worktree patch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opencollab_eval.engine.swe_generation_proof import (
    MAX_TRUSTED_PATCH_BYTES,
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_EXTRACTED_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
    TRUSTED_PATCH_EXTRACTION_SCHEMA,
)
from opencollab_eval.engine.workspace_integrity import (
    FailureScope,
    FindingOrigin,
    IntegrityPhase,
    WorkspaceChange,
    WorkspaceFinding,
    WorkspaceIntegrityError,
    classify_finding,
    failure_report,
)
from opencollab_eval.patch_diff import (
    patch_entries,
    remove_generated_artifact_blocks,
    split_patch_blocks,
)
from opencollab_eval.patch_paths import is_generated_runtime_artifact_path

from .candidate_gitlinks import derive_gitlink_projections, visible_unmaterialized_gitlinks
from .candidate_patch import CandidatePatch, GitlinkProjection, construct_candidate_patch
from .candidate_patch_git import project_candidate_patch
from .container_quiescence import frozen_container, require_container_quiescence
from .gen_prediction_config import _docker_timeout_from_env
from .gen_prediction_constants import DOCKER_WORKDIR
from .gen_prediction_patch_git import (
    gitlink_repository_digest as _gitlink_repository_digest,
)
from .gen_prediction_patch_git import (
    prepare_gitlink_state_repositories as _prepare_gitlink_state_repositories,
)
from .gen_prediction_patch_git import (
    run_git as _run_git,
)
from .gen_prediction_patch_git import strip_nested_git_metadata as _strip_nested_git_metadata
from .gen_prediction_snapshot import SolverGitSnapshot
from .gen_prediction_snapshot_support import workspace_sha256


@dataclass(frozen=True, slots=True)
class TrustedPatchExtraction:
    fixed_anonymous_base: str
    base_tree: str
    baseline_archive_sha256: str
    baseline_archive_bytes: int
    baseline_archive_entries: int
    baseline_extracted_bytes: int
    workspace_archive_sha256: str
    workspace_archive_bytes: int
    workspace_archive_entries: int
    workspace_extracted_bytes: int
    patch_sha256: str
    patch_bytes: int
    candidate_tree: str
    changed_paths: tuple[str, ...]
    path_modes: tuple[tuple[str, str, str], ...]
    workspace_integrity: dict[str, object] | None = None

    def as_dict(self) -> dict[str, str | int | bool]:
        integrity = self.workspace_integrity or {
            "schema": "opencollab.workspace_integrity.v1",
            "findings": [],
            "outcome": "allow",
            "failure_scope": "none",
        }
        return {
            "schema": TRUSTED_PATCH_EXTRACTION_SCHEMA,
            "host_trusted": True,
            "fixed_anonymous_base": self.fixed_anonymous_base,
            "base_tree": self.base_tree,
            "archive_bounded": True,
            "baseline_archive_sha256": self.baseline_archive_sha256,
            "baseline_archive_bytes": self.baseline_archive_bytes,
            "baseline_archive_entries": self.baseline_archive_entries,
            "baseline_extracted_bytes": self.baseline_extracted_bytes,
            "workspace_archive_sha256": self.workspace_archive_sha256,
            "workspace_archive_bytes": self.workspace_archive_bytes,
            "workspace_archive_entries": self.workspace_archive_entries,
            "workspace_extracted_bytes": self.workspace_extracted_bytes,
            "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
            "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
            "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
            "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
            "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
            "container_quiesced_before": True,
            "container_quiesced_after": True,
            "workspace_frozen_during_copy": True,
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "candidate_tree": self.candidate_tree,
            "changed_paths": list(self.changed_paths),
            "path_modes": [
                {"path": path, "old_mode": old, "new_mode": new}
                for path, old, new in self.path_modes
            ],
            "workspace_integrity": integrity,
        }


@dataclass(slots=True)
class TrustedPatchBaseline:
    snapshot: SolverGitSnapshot
    temporary_directory: tempfile.TemporaryDirectory[str] | None
    git_dir: Path
    archive_sha256: str
    archive_bytes: int
    archive_entries: int
    extracted_bytes: int
    gitlink_worktrees: tuple[tuple[str, str | None], ...] = ()
    gitlink_state_repositories: tuple[tuple[str, Path], ...] = ()

    def cleanup(self) -> None:
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()


class _BoundedHashReader:
    def __init__(self, raw, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        allowed = self.limit - self.count
        if allowed < 0:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        request = allowed + 1 if size < 0 else min(size, allowed + 1)
        data = self.raw.read(request)
        if not data:
            return b""
        self.count += len(data)
        if self.count > self.limit:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        self.digest.update(data)
        return data


def _member_parts(name: str) -> tuple[str, ...]:
    if "\x00" in name:
        raise RuntimeError("container workspace archive contains a NUL path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise RuntimeError("container workspace archive contains an absolute path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return ()
    if any(part == ".." for part in parts):
        raise RuntimeError("container workspace archive escapes the extraction root")
    return parts


def _safe_parent(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise RuntimeError("container workspace archive traverses a non-directory parent")
    return root.joinpath(*parts)


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root: Path,
    *,
    extracted_bytes: int,
    directory_modes: dict[tuple[str, ...], int] | None = None,
    pending_hardlinks: list[tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
) -> int:
    parts = _member_parts(member.name)
    if not parts:
        if member.isdir():
            return extracted_bytes
        raise RuntimeError("container workspace archive has a non-directory root entry")
    destination = _safe_parent(root, parts)
    if member.isdir():
        if directory_modes is not None:
            directory_modes[parts] = member.mode & 0o777
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            destination.mkdir()
        else:
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise RuntimeError("container workspace archive redefines a path as a directory")
        return extracted_bytes
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("container workspace archive contains a duplicate path")
    if member.issym():
        if "\x00" in member.linkname:
            raise RuntimeError("container workspace archive contains an invalid symlink")
        os.symlink(member.linkname, destination)
        return extracted_bytes
    if member.islnk():
        target_parts = _member_parts(member.linkname)
        if not target_parts or pending_hardlinks is None:
            raise RuntimeError("container workspace archive contains an invalid hard link")
        pending_hardlinks.append((parts, target_parts))
        return extracted_bytes
    if not member.isfile():
        os.mkfifo(destination, member.mode & 0o777)
        return extracted_bytes
    if member.size < 0 or member.size > MAX_WORKSPACE_FILE_BYTES:
        raise RuntimeError("container workspace archive contains an oversized file")
    total = extracted_bytes + member.size
    if total > MAX_WORKSPACE_EXTRACTED_BYTES:
        raise RuntimeError("container workspace extraction exceeded its byte limit")
    source = archive.extractfile(member)
    if source is None:
        raise RuntimeError("container workspace archive file payload is missing")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, member.mode & 0o777 or 0o600)
    written = 0
    try:
        while written < member.size:
            chunk = source.read(min(1024 * 1024, member.size - written))
            if not chunk:
                raise RuntimeError("container workspace archive file payload is truncated")
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            written += len(chunk)
        os.fchmod(fd, member.mode & 0o777)
        os.fsync(fd)
    finally:
        os.close(fd)
    return total


def _materialize_hardlinks(
    root: Path,
    pending: list[tuple[tuple[str, ...], tuple[str, ...]]],
) -> None:
    remaining = list(pending)
    while remaining:
        deferred: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        progress = False
        for destination_parts, target_parts in remaining:
            destination = _safe_parent(root, destination_parts)
            target = root.joinpath(*target_parts)
            if not os.path.lexists(target):
                deferred.append((destination_parts, target_parts))
                continue
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("container workspace archive hard link target is unsafe")
            os.link(target, destination, follow_symlinks=False)
            progress = True
        if deferred and not progress:
            raise RuntimeError("container workspace archive hard link target is missing")
        remaining = deferred


def _restore_directory_modes(root: Path, directory_modes: dict[tuple[str, ...], int]) -> None:
    for parts, mode in sorted(directory_modes.items(), key=lambda item: len(item[0]), reverse=True):
        if not parts:
            continue
        directory = root.joinpath(*parts)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("container workspace archive directory changed type during extraction")
        os.chmod(directory, mode)


def _copy_workspace_archive(container_id: str, root: Path) -> tuple[str, int, int, int]:
    command = ["docker", "cp", f"{container_id}:{DOCKER_WORKDIR}/.", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        process.kill()
        raise RuntimeError("docker cp did not expose its archive stream")
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(_docker_timeout_from_env(), kill_on_timeout)
    reader = _BoundedHashReader(process.stdout, MAX_WORKSPACE_ARCHIVE_BYTES)
    entries = 0
    extracted = 0
    directory_modes: dict[tuple[str, ...], int] = {}
    pending_hardlinks: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    timer.start()
    try:
        with tarfile.open(fileobj=reader, mode="r|*") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_WORKSPACE_ARCHIVE_ENTRIES:
                    raise RuntimeError("container workspace archive exceeded its entry limit")
                extracted = _extract_member(
                    archive,
                    member,
                    root,
                    extracted_bytes=extracted,
                    directory_modes=directory_modes,
                    pending_hardlinks=pending_hardlinks,
                )
        _materialize_hardlinks(root, pending_hardlinks)
        _restore_directory_modes(root, directory_modes)
        while reader.read(1024 * 1024):
            pass
        returncode = process.wait(timeout=5)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if timed_out.is_set():
        raise RuntimeError("container workspace archive copy timed out")
    if returncode != 0:
        raise RuntimeError(f"docker cp workspace archive failed with exit {returncode}")
    return reader.digest.hexdigest(), reader.count, entries, extracted


def _git_environment(home: Path, template: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TEMPLATE_DIR": str(template),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "xdg"),
            "LC_ALL": "C",
        }
    )
    return env


def _copy_object_store(source_git: Path, clean_git: Path, object_id_length: int) -> None:
    source_objects = source_git / "objects"
    if not source_objects.is_dir() or source_objects.is_symlink():
        raise RuntimeError("solver object store is missing or unsafe")
    copied = 0
    suffix_length = object_id_length - 2
    loose_object = re.compile(rf"[0-9a-f]{{2}}/[0-9a-f]{{{suffix_length}}}\Z")
    packed_object = re.compile(
        rf"pack/pack-[0-9a-f]{{{object_id_length}}}\.(?:idx|pack)\Z"
    )
    for source in source_objects.rglob("*"):
        if source.is_dir() and not source.is_symlink():
            continue
        relative = source.relative_to(source_objects).as_posix()
        if not (loose_object.fullmatch(relative) or packed_object.fullmatch(relative)):
            continue
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("solver object store contains an unsafe object file")
        destination = clean_git / "objects" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied += 1
    if copied == 0:
        raise RuntimeError("solver object store did not contain any usable objects")


def _visible_gitlink_projections(
    root: Path,
    baseline: TrustedPatchBaseline,
    git: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> tuple[GitlinkProjection, ...]:
    expected_digests = dict(baseline.gitlink_worktrees)
    baseline_repositories = dict(baseline.gitlink_state_repositories)
    entries = tuple(
        (path, oid, expected_digests.get(path), baseline_repositories.get(path))
        for path, oid in baseline.snapshot.removed_gitlinks
    )
    visible = visible_unmaterialized_gitlinks(
        git=git,
        git_dir=baseline.git_dir,
        worktree=root,
        base=baseline.snapshot.anonymous_head,
        paths=tuple(path for path, _oid, digest, _repository in entries if digest is None),
        env=env,
        timeout=timeout,
    )
    return derive_gitlink_projections(
        worktree=root,
        entries=entries,
        git=git,
        env=env,
        timeout=timeout,
        visible_unmaterialized=visible,
    )


def _construct_candidate_from_copy(root: Path, baseline: TrustedPatchBaseline) -> CandidatePatch:
    snapshot = baseline.snapshot
    git = shutil.which("git")
    if not git:
        raise RuntimeError("trusted host git executable is unavailable")
    timeout = _docker_timeout_from_env()
    with tempfile.TemporaryDirectory(prefix="opencollab-host-git-") as temp:
        temp_root = Path(temp)
        home = temp_root / "home"
        template = temp_root / "template"
        home.mkdir()
        (home / "xdg").mkdir()
        template.mkdir()
        env = _git_environment(home, template)
        projections = _visible_gitlink_projections(
            root, baseline, git, env=env, timeout=timeout
        )
    return construct_candidate_patch(
        git_dir=baseline.git_dir,
        worktree=root,
        base=snapshot.anonymous_head,
        baseline_sha256=baseline.archive_sha256,
        max_patch_bytes=MAX_TRUSTED_PATCH_BYTES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_census_bytes=MAX_WORKSPACE_ARCHIVE_BYTES,
        max_census_entries=MAX_WORKSPACE_ARCHIVE_ENTRIES,
        timeout=timeout,
        gitlinks=projections,
    )


def _extract_patch_from_copy(root: Path, baseline: TrustedPatchBaseline) -> str:
    return _construct_candidate_from_copy(root, baseline).patch


def prepare_trusted_patch_baseline(
    container_id: str,
    snapshot: SolverGitSnapshot,
) -> TrustedPatchBaseline:
    """Capture and verify the anonymous object store before Solver execution."""
    require_container_quiescence(container_id)
    temporary_directory = tempfile.TemporaryDirectory(prefix="opencollab-trusted-base-")
    temp_root = Path(temporary_directory.name)
    try:
        root = temp_root / "workspace"
        root.mkdir()
        with frozen_container(container_id):
            archive_sha, archive_bytes, entries, extracted_bytes = _copy_workspace_archive(
                container_id,
                root,
            )
        require_container_quiescence(container_id)
        _strip_nested_git_metadata(root)
        source_git = root / ".git"
        if not source_git.is_dir() or source_git.is_symlink():
            raise RuntimeError("trusted baseline Git directory is missing or unsafe")
        isolated_source_git = temp_root / "source.git"
        source_git.rename(isolated_source_git)
        if workspace_sha256(root) != snapshot.workspace_sha256:
            raise RuntimeError("trusted baseline workspace does not match snapshot evidence")
        source_git = isolated_source_git
        git = shutil.which("git")
        if not git:
            raise RuntimeError("trusted host git executable is unavailable")
        clean_git = temp_root / "repo.git"
        home = temp_root / "home"
        template = temp_root / "template"
        home.mkdir()
        (home / "xdg").mkdir()
        template.mkdir()
        env = _git_environment(home, template)
        object_format_args = (
            ["--object-format=sha256"] if len(snapshot.anonymous_head) == 64 else []
        )
        timeout = _docker_timeout_from_env()
        gitlink_state_repositories = _prepare_gitlink_state_repositories(
            root,
            snapshot,
            temp_root / "gitlinks",
            git,
            env=env,
            timeout=timeout,
        )
        gitlink_worktrees = tuple(
            (
                path,
                _gitlink_repository_digest(repository, git, env=env, timeout=timeout),
            )
            for path, repository in gitlink_state_repositories
        )
        _run_git(
            git,
            [
                "init",
                "--bare",
                "--quiet",
                *object_format_args,
                str(clean_git),
            ],
            env=env,
            timeout=timeout,
        )
        _copy_object_store(source_git, clean_git, len(snapshot.anonymous_head))
        common = [f"--git-dir={clean_git}"]
        _run_git(
            git,
            [*common, "cat-file", "-e", f"{snapshot.anonymous_head}^{{commit}}"],
            env=env,
            timeout=timeout,
        )
        tree = subprocess.run(
            [git, *common, "rev-parse", f"{snapshot.anonymous_head}^{{tree}}"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        if tree.returncode != 0 or tree.stdout.strip().lower() != snapshot.base_tree:
            raise RuntimeError("trusted host base tree does not match snapshot evidence")
        shutil.rmtree(root)
        return TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=temporary_directory,
            git_dir=clean_git,
            archive_sha256=archive_sha,
            archive_bytes=archive_bytes,
            archive_entries=entries,
            extracted_bytes=extracted_bytes,
            gitlink_worktrees=gitlink_worktrees,
            gitlink_state_repositories=gitlink_state_repositories,
        )
    except BaseException:
        temporary_directory.cleanup()
        raise


def extract_patch_trusted(
    container_id: str,
    baseline: TrustedPatchBaseline,
) -> tuple[str, TrustedPatchExtraction]:
    """Copy a quiet container and diff it against its pre-Solver anonymous base."""
    try:
        require_container_quiescence(container_id)
    except WorkspaceIntegrityError:
        raise
    except Exception as exc:
        finding = WorkspaceFinding(
            kind="background_write",
            phase=IntegrityPhase.POST_SOLVER,
            origin=FindingOrigin.UNKNOWN,
            change=WorkspaceChange.MODIFIED,
            solver_readable=True,
            candidate_effect=True,
            evidence_effect=True,
            representable_in_patch=False,
            detail=f"{type(exc).__name__}: {exc}"[:1000],
        )
        raise WorkspaceIntegrityError(
            f"container did not become quiet before patch extraction: {exc}",
            scope=FailureScope.TASK,
            report=failure_report(
                finding,
                verified_state_after_action="patch_extraction_blocked_before_copy",
            ),
        ) from exc
    try:
        with tempfile.TemporaryDirectory(prefix="opencollab-workspace-copy-") as temp:
            root = Path(temp) / "workspace"
            root.mkdir()
            with frozen_container(container_id):
                archive_sha, archive_bytes, entries, extracted = _copy_workspace_archive(
                    container_id,
                    root,
                )
            require_container_quiescence(container_id)
            candidate = _construct_candidate_from_copy(root, baseline)
            patch = candidate.patch
    except WorkspaceIntegrityError:
        raise
    except Exception as exc:
        finding = WorkspaceFinding(
            kind="candidate_workspace_change",
            phase=IntegrityPhase.POST_SOLVER,
            origin=FindingOrigin.UNKNOWN,
            change=WorkspaceChange.MODIFIED,
            solver_readable=True,
            candidate_effect=True,
            evidence_effect=True,
            representable_in_patch=False,
            detail=f"{type(exc).__name__}: {exc}"[:1000],
        )
        raise WorkspaceIntegrityError(
            f"candidate workspace could not produce a stable trusted patch: {exc}",
            scope=FailureScope.TASK,
            report=failure_report(
                finding,
                verified_state_after_action="candidate_patch_not_published",
            ),
        ) from exc
    encoded = patch.encode("utf-8", errors="surrogatepass")
    finding = WorkspaceFinding(
        kind="candidate_workspace_change",
        phase=IntegrityPhase.POST_SOLVER,
        origin=FindingOrigin.CURRENT_RUN,
        change=WorkspaceChange.MODIFIED if patch.strip() else WorkspaceChange.UNCHANGED,
        candidate_effect=bool(patch.strip()),
        representable_in_patch=True,
    )
    decision = classify_finding(finding)
    integrity = {
        "schema": "opencollab.workspace_integrity.v1",
        "findings": [
            {
                "observed_state": finding.as_dict(),
                "classification_basis": decision.basis,
                "action": decision.action.value,
                "verified_state_after_action": "bound_to_extracted_patch_sha256",
                "failure_scope": decision.scope.value,
            }
        ],
        "outcome": decision.action.value,
        "failure_scope": FailureScope.NONE.value,
    }
    proof = TrustedPatchExtraction(
        fixed_anonymous_base=baseline.snapshot.anonymous_head,
        base_tree=baseline.snapshot.base_tree,
        baseline_archive_sha256=baseline.archive_sha256,
        baseline_archive_bytes=baseline.archive_bytes,
        baseline_archive_entries=baseline.archive_entries,
        baseline_extracted_bytes=baseline.extracted_bytes,
        workspace_archive_sha256=archive_sha,
        workspace_archive_bytes=archive_bytes,
        workspace_archive_entries=entries,
        workspace_extracted_bytes=extracted,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
        candidate_tree=candidate.candidate_tree,
        changed_paths=candidate.changed_paths,
        path_modes=candidate.path_modes,
        workspace_integrity=integrity,
    )
    return patch, proof


def _new_generated_artifact_paths(patch: str) -> set[str]:
    generated: set[str] = set()
    for block in split_patch_blocks(patch):
        if not any(line.startswith("new file mode ") for line in block):
            continue
        entries = patch_entries("".join(block))
        if len(entries) != 1:
            continue
        endpoints = {path for path in entries[0] if path}
        if len(endpoints) == 1 and all(
            is_generated_runtime_artifact_path(path) for path in endpoints
        ):
            generated.update(endpoints)
    return generated


def extract_patch_guarded(
    container_id: str,
    baseline: TrustedPatchBaseline,
) -> tuple[str, list[str], dict]:
    """Extract one candidate and exclude only newly generated runtime artifacts."""
    patch, extraction = extract_patch_trusted(container_id, baseline)
    generated_artifacts = _new_generated_artifact_paths(patch)
    filtered_patch, removed = remove_generated_artifact_blocks(patch, generated_artifacts)
    proof = extraction.as_dict()
    if not removed:
        return filtered_patch, [], proof
    proof["pre_sanitization_patch_sha256"] = proof["patch_sha256"]
    git = shutil.which("git")
    if not git:
        raise RuntimeError("trusted candidate Git executable is unavailable")
    pre_tree, _pre_patch, _pre_paths, _pre_modes = project_candidate_patch(
        git=git,
        git_dir=baseline.git_dir,
        base=baseline.snapshot.anonymous_head,
        patch=patch,
    )
    tree, canonical, paths, modes = project_candidate_patch(
        git=git,
        git_dir=baseline.git_dir,
        base=baseline.snapshot.anonymous_head,
        patch=filtered_patch,
    )
    encoded = canonical.encode("utf-8", errors="surrogatepass")
    proof.update(
        pre_sanitization_candidate_tree=pre_tree,
        candidate_tree=tree,
        changed_paths=list(paths),
        path_modes=[
            {"path": path, "old_mode": old, "new_mode": new}
            for path, old, new in modes
        ],
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    )
    integrity = dict(proof["workspace_integrity"])
    integrity["findings"] = [
        *integrity.get("findings", []),
        {
            "observed_state": {
                "kind": "new_generated_runtime_artifact",
                "phase": "post_solver",
                "origin": "current_run",
                "change": "added",
                "detail": json.dumps(sorted(removed), ensure_ascii=True),
            },
            "classification_basis": "new runtime artifact has no candidate or test semantics",
            "action": "sanitize_then_continue",
            "verified_state_after_action": "excluded_from_candidate_patch",
            "failure_scope": "none",
        },
    ]
    integrity["outcome"] = "sanitize_then_continue"
    proof["workspace_integrity"] = integrity
    return canonical, removed, proof


__all__ = [
    "TrustedPatchBaseline",
    "TrustedPatchExtraction",
    "extract_patch_guarded",
    "extract_patch_trusted",
    "prepare_trusted_patch_baseline",
]
