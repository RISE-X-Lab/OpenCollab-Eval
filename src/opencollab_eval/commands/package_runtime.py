"""Package the installed OC and OCE sources for a server-local evaluator."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from opencollab_eval.commands.swe_v1_prolite_config import (
    _runtime_directory_sources,
    runtime_tree_identity,
    verify_runtime_manifest,
)


def package_runtime(output: Path) -> dict:
    """Use the existing runtime format and require a fresh destination."""
    sources, oc_version = _runtime_directory_sources()
    output = output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False)
    members = []
    for relative, source in sources.items():
        for path in sorted(source.rglob("*")):
            parts = path.relative_to(source).parts
            if any(part in {"__pycache__", ".git", ".pytest_cache", ".ruff_cache"} for part in parts):
                continue
            if path.suffix in {".pyc", ".orig", ".rej"} or path.name == ".env":
                continue
            if path.is_symlink():
                raise ValueError(f"runtime source must be a regular file or directory: {path}")
            if not path.is_file():
                continue
            member = (Path(relative) / path.relative_to(source)).as_posix()
            destination = output / member
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            members.append(member)
    members.sort()
    manifest = {
        "version": 2,
        "synced": [],
        "synced_dirs": sorted(sources),
        "archive_members": members,
        "source_tree": runtime_tree_identity(output, members),
        "opencollab": {"distribution_version": oc_version, "public_api_version": 1},
    }
    (output / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    identity = verify_runtime_manifest(output)
    return {"output": str(output), "files": len(members), "source_tree": identity, "model_calls": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(package_runtime(args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
