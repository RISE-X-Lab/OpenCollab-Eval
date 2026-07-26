"""Host-side installation and verification of solver Git snapshots."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from opencollab_eval.engine.swe_generation_proof import solver_git_snapshot_valid
from opencollab_eval.engine.workspace_integrity import (
    FailureScope,
    WorkspaceIntegrityError,
)

from .gen_prediction_config import _docker_timeout_from_env
from .gen_prediction_constants import DOCKER_WORKDIR
from .gen_prediction_docker import _check_docker, _docker

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_CONTAINER_HELPER = "/tmp/opencollab_gen_prediction_snapshot.py"
_CONTAINER_HELPER_SOURCE = Path(__file__).with_name("gen_prediction_snapshot_container.py")
_CONTAINER_POLICY_HELPER = "/tmp/opencollab_workspace_integrity.py"
_CONTAINER_POLICY_HELPER_SOURCE = (
    Path(__file__).parents[1] / "engine" / "workspace_integrity.py"
)
_CONTAINER_SUPPORT_HELPER = "/tmp/opencollab_snapshot_support.py"
_CONTAINER_SUPPORT_HELPER_SOURCE = Path(__file__).with_name("gen_prediction_snapshot_support.py")
_MAX_EVIDENCE_BYTES = 256 * 1024


def _docker_with_stdin(*args: str, input_text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=_docker_timeout_from_env(),
        check=False,
    )


@dataclass(frozen=True)
class SolverGitSnapshot:
    anonymous_head: str
    base_tree: str
    commit_count: int
    remote_count: int
    extra_git_metadata: int
    removed_git_metadata: int
    removed_gitlinks: tuple[tuple[str, str], ...] = ()
    expected_base_commit: str = ""
    workspace_integrity: dict[str, object] | None = None
    workspace_sha256: str = "0" * 64
    materialized_gitlinks: tuple[tuple[str, str, str], ...] = ()

    def as_dict(self) -> dict[str, object]:
        integrity = self.workspace_integrity or {
            "schema": "opencollab.workspace_integrity.v1",
            "findings": [],
            "outcome": "allow",
            "failure_scope": "none",
        }
        return {
            "enabled": True,
            "anonymous_head": self.anonymous_head,
            "base_tree": self.base_tree,
            "workspace_sha256": self.workspace_sha256,
            "commit_count": self.commit_count,
            "remote_count": self.remote_count,
            "extra_git_metadata": self.extra_git_metadata,
            "removed_git_metadata": self.removed_git_metadata,
            "removed_gitlinks": [
                {"path": path, "old_oid": old_oid}
                for path, old_oid in self.removed_gitlinks
            ],
            "materialized_gitlinks": [
                {"path": path, "oid": oid, "content_sha256": content_sha256}
                for path, oid, content_sha256 in self.materialized_gitlinks
            ],
            "expected_base_commit": self.expected_base_commit or self.anonymous_head,
            "workspace_integrity": integrity,
        }


def _parse_snapshot_output(output: str) -> SolverGitSnapshot:
    if len(output.encode("utf-8", errors="surrogatepass")) > _MAX_EVIDENCE_BYTES:
        raise RuntimeError("solver Git snapshot evidence exceeded its size bound")
    try:
        values = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("solver Git snapshot returned malformed evidence") from exc
    if not solver_git_snapshot_valid(values):
        raise RuntimeError("solver Git snapshot returned invalid evidence")
    removed_gitlinks = values["removed_gitlinks"]
    materialized_gitlinks = values["materialized_gitlinks"]
    snapshot = SolverGitSnapshot(
        anonymous_head=str(values["anonymous_head"]).lower(),
        base_tree=str(values["base_tree"]).lower(),
        commit_count=values["commit_count"],
        remote_count=values["remote_count"],
        extra_git_metadata=values["extra_git_metadata"],
        removed_git_metadata=values["removed_git_metadata"],
        removed_gitlinks=tuple(
            (item["path"], item["old_oid"].lower())
            for item in removed_gitlinks
        ),
        expected_base_commit=values["expected_base_commit"].lower(),
        workspace_integrity=values["workspace_integrity"],
        workspace_sha256=values["workspace_sha256"],
        materialized_gitlinks=tuple(
            (item["path"], item["oid"].lower(), item["content_sha256"])
            for item in materialized_gitlinks
        ),
    )
    if snapshot.commit_count != 1 or snapshot.remote_count != 0 or snapshot.extra_git_metadata != 0:
        raise RuntimeError("solver Git snapshot integrity verification failed")
    return snapshot


def prepare_solver_git_snapshot(
    container_id: str,
    expected_base_commit: str,
    *,
    workspace: str = DOCKER_WORKDIR,
) -> SolverGitSnapshot:
    """Replace the image's Git history with one anonymous base-tree commit."""
    expected_base_commit = str(expected_base_commit or "").strip()
    if _COMMIT_RE.fullmatch(expected_base_commit) is None:
        raise ValueError("expected base commit must be a full hexadecimal object id")
    for source, destination in (
        (_CONTAINER_POLICY_HELPER_SOURCE, _CONTAINER_POLICY_HELPER),
        (_CONTAINER_SUPPORT_HELPER_SOURCE, _CONTAINER_SUPPORT_HELPER),
        (_CONTAINER_HELPER_SOURCE, _CONTAINER_HELPER),
    ):
        install = _docker("cp", str(source), f"{container_id}:{destination}")
        _check_docker(install, "solver Git snapshot helper installation")
    result = _docker_with_stdin(
        "exec",
        "-i",
        "-w",
        "/tmp",
        container_id,
        "python3",
        _CONTAINER_HELPER,
        workspace,
        input_text=expected_base_commit.lower() + "\n",
    )
    if result.returncode != 0:
        try:
            payload = json.loads(result.stdout)
            report = payload["workspace_integrity"]
            scope = FailureScope(str(report["failure_scope"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            _check_docker(result, "solver Git snapshot setup")
            raise AssertionError("unreachable") from None
        detail = result.stderr.strip() or "solver Git snapshot setup failed"
        raise WorkspaceIntegrityError(detail[:4000], scope=scope, report=report)
    _check_docker(result, "solver Git snapshot setup")
    return _parse_snapshot_output(result.stdout)


def anonymous_solver_task_id() -> str:
    """Return an opaque per-attempt task id that carries no dataset identity."""
    return "solver-" + secrets.token_hex(16)
