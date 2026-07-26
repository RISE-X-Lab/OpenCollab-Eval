"""Capture and project controller-owned Gitlink state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .candidate_gitlinks import (
    capture_gitlink_manifest,
    project_gitlink_manifest,
    read_manifest,
    replay_gitlink_paths,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    for command in (capture,):
        command.add_argument("--git-dir", type=Path, required=True)
        command.add_argument("--base", required=True)
        command.add_argument("--base-tree", required=True)
        command.add_argument("--baseline-sha256", required=True)
    capture.add_argument("--work-tree", type=Path, required=True)
    capture.add_argument("--repository-directory", type=Path, required=True)
    project = commands.add_parser("project")
    project.add_argument("--manifest", type=Path, required=True)
    project.add_argument("--work-tree", type=Path, required=True)
    project.add_argument("--git-dir", type=Path, required=True)
    project.add_argument("--repository-directory", type=Path, required=True)
    replay = commands.add_parser("replay-paths")
    replay.add_argument("--manifest", type=Path, required=True)
    for command in (capture, project):
        command.add_argument("--output", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        value = capture_gitlink_manifest(
            git_dir=args.git_dir,
            worktree=args.work_tree,
            base=args.base,
            base_tree=args.base_tree,
            baseline_sha256=args.baseline_sha256,
            repository_directory=args.repository_directory,
        )
    elif args.command == "project":
        value = project_gitlink_manifest(
            manifest=read_manifest(args.manifest),
            worktree=args.work_tree,
            git_dir=args.git_dir,
            repository_directory=args.repository_directory,
        )
    else:
        paths = replay_gitlink_paths(read_manifest(args.manifest))
        args.output.write_bytes(b"\0".join(path.encode() for path in paths) + (b"\0" if paths else b""))
        return
    args.output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
