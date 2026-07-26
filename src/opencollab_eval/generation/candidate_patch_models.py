"""Evidence models for trusted candidate construction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitlinkProjection:
    path: str
    oid: str
    action: str
    baseline_digest: str | None = None
    current_digest: str | None = None
    ignored_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidatePatch:
    patch: str
    patch_sha256: str
    anonymous_base: str
    base_tree: str
    baseline_sha256: str
    candidate_tree: str
    changed_paths: tuple[str, ...]
    path_modes: tuple[tuple[str, str, str], ...]
    untracked_paths: tuple[str, ...]
    excluded_harness_paths: tuple[str, ...]
    flattened_repositories: tuple[tuple[str, str], ...]
    flattened_hardlinks: tuple[str, ...]
    census_bytes: int
    census_entries: int
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "opencollab.candidate_patch.v1",
            "base_source": "controller_owned_git",
            "index_source": "controller_temporary_index",
            "solver_git_metadata_used": False,
            "ignored_files_forced": False,
            "anonymous_base": self.anonymous_base,
            "base_tree": self.base_tree,
            "baseline_sha256": self.baseline_sha256,
            "patch_sha256": self.patch_sha256,
            "patch_bytes": len(self.patch.encode()),
            "candidate_tree": self.candidate_tree,
            "changed_paths": list(self.changed_paths),
            "path_modes": [
                {"path": path, "old_mode": old, "new_mode": new}
                for path, old, new in self.path_modes
            ],
            "untracked_paths": list(self.untracked_paths),
            "excluded_harness_paths": list(self.excluded_harness_paths),
            "flattened_repositories": [
                {"path": path, "marker_type": marker_type}
                for path, marker_type in self.flattened_repositories
            ],
            "flattened_hardlinks": list(self.flattened_hardlinks),
            "census_bytes": self.census_bytes,
            "census_entries": self.census_entries,
            "status_sha256": hashlib.sha256(self.status.encode()).hexdigest(),
        }
