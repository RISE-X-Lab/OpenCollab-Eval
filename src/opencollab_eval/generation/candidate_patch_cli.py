"""Command-line entry point for trusted candidate construction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opencollab_eval.engine.swe_generation_proof import (
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_FILE_BYTES,
)

from .candidate_patch import GitlinkProjection, construct_candidate_patch

_MAX_MANIFEST_BYTES = 1024 * 1024


def _gitlinks(
    path: Path | None, *, base: str, base_tree: str, baseline_sha256: str
) -> tuple[GitlinkProjection, ...]:
    if path is None:
        return ()
    payload = path.read_bytes()
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise ValueError("Gitlink projection manifest exceeded its byte limit")
    value = json.loads(payload)
    items = value.get("gitlinks") if isinstance(value, dict) else None
    if (
        not isinstance(items, list)
        or len(items) > 10_000
        or value.get("schema") != "opencollab.candidate_gitlink_projections.v1"
        or value.get("anonymous_base") != base
        or value.get("base_tree") != base_tree
        or value.get("baseline_sha256") != baseline_sha256
    ):
        raise ValueError("Gitlink projection manifest is invalid")
    projections: list[GitlinkProjection] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "path", "oid", "action", "baseline_digest", "current_digest", "ignored_paths"
        }:
            raise ValueError("Gitlink projection entry is invalid")
        ignored = item.get("ignored_paths")
        if not isinstance(ignored, list) or any(not isinstance(value, str) for value in ignored):
            raise ValueError("Gitlink projection ignored paths are invalid")
        projections.append(GitlinkProjection(**{**item, "ignored_paths": tuple(ignored)}))
    return tuple(projections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-dir", type=Path, required=True)
    parser.add_argument("--work-tree", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-tree", required=True)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--gitlink-projections", type=Path)
    parser.add_argument("--max-patch-bytes", type=int, required=True)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_WORKSPACE_FILE_BYTES)
    parser.add_argument("--max-census-bytes", type=int, default=MAX_WORKSPACE_ARCHIVE_BYTES)
    parser.add_argument("--max-census-entries", type=int, default=MAX_WORKSPACE_ARCHIVE_ENTRIES)
    parser.add_argument("--patch-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--status-output", type=Path, required=True)
    args = parser.parse_args()
    result = construct_candidate_patch(
        git_dir=args.git_dir,
        worktree=args.work_tree,
        base=args.base,
        baseline_sha256=args.baseline_sha256,
        max_patch_bytes=args.max_patch_bytes,
        max_file_bytes=args.max_file_bytes,
        max_census_bytes=args.max_census_bytes,
        max_census_entries=args.max_census_entries,
        gitlinks=_gitlinks(
            args.gitlink_projections,
            base=args.base,
            base_tree=args.base_tree,
            baseline_sha256=args.baseline_sha256,
        ),
    )
    if result.base_tree != args.base_tree:
        raise ValueError("trusted candidate base tree does not match the requested identity")
    args.patch_output.write_text(result.patch, encoding="utf-8")
    args.manifest_output.write_text(
        json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.status_output.write_text(result.status, encoding="utf-8")


if __name__ == "__main__":
    main()
