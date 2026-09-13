"""Keep public image dependencies available around source-only snapshots."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

if __package__:
    from opencollab_eval.engine import eval_runtime_dependencies as runtime
else:
    import opencollab_eval_runtime_dependencies as runtime

_LOCK_FILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb"}


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout


def _nodebb_public_runtime_roots(repo: Path, base: str, tracked: set[str]) -> set[str]:
    """Retain NodeBB's public image setup around the source-only snapshot."""
    if "install/package.json" not in tracked or "package.json" in tracked:
        return set()
    package = repo / "package.json"
    if not package.is_file() or package.is_symlink():
        return set()
    public_package = _git(repo, "show", f"{base}:install/package.json")
    try:
        is_nodebb = json.loads(public_package).get("name") == "nodebb"
    except (ValueError, AttributeError):
        return set()
    if not is_nodebb or package.read_bytes() != public_package:
        return set()
    return {
        "package.json",
        "package-lock.json",
        "node_modules",
        "config.json",
        "build/public",
        "build/cache-buster",
        "build/active_plugins.json",
    }


def discover_specs(repo: Path, base: str) -> list[dict[str, object]]:
    """Select cached packages and ignored assets declared by public base files."""
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base) is None:
        raise ValueError("expected base must be a full Git object id")
    tracked = {
        os.fsdecode(value) for value in _git(repo, "ls-tree", "-r", "--name-only", "-z", base).split(b"\0") if value
    }
    roots = _nodebb_public_runtime_roots(repo, base, tracked)
    for name in tracked:
        path = PurePosixPath(name)
        if path.name in _LOCK_FILES and str(path.parent / "package.json") in tracked:
            roots.add(str(path.parent / "node_modules"))
        if path.suffix != ".go":
            continue
        content = _git(repo, "show", f"{base}:{name}").decode("utf-8", errors="replace")
        for line in content.splitlines():
            if not line.startswith("//go:embed "):
                continue
            for pattern in shlex.split(line[len("//go:embed ") :]):
                pattern = pattern.removeprefix("all:")
                relative = PurePosixPath(pattern)
                if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                    continue
                for match in (repo / path.parent).glob(pattern):
                    roots.add(match.relative_to(repo).as_posix())
    selected = []
    for name in sorted(roots, key=lambda value: (len(PurePosixPath(value).parts), value)):
        path = repo / name
        if any(name == old or name.startswith(old + "/") for old in selected):
            continue
        if name in tracked or any(value.startswith(name + "/") for value in tracked):
            continue
        if not path.exists() or path.is_symlink() or not runtime._ignored(repo, name):
            continue
        if any(parent.is_symlink() for parent in path.parents if parent != repo and repo in parent.parents):
            continue
        if path.is_file() or path.is_dir():
            selected.append(name)
    return [
        {
            "root": name,
            "required_paths": [name],
            "kind": "directory" if (repo / name).is_dir() else "file",
            "candidate_protected": True,
        }
        for name in selected
    ]


def stash_image_dependencies(repo: Path, base: str, store: Path) -> list[str]:
    specs = discover_specs(repo, base)
    store.mkdir(mode=0o700)
    for offset in range(0, len(specs), 16):
        spec_path = store / f"spec-{offset // 16}.json"
        spec_path.write_text(json.dumps(specs[offset : offset + 16]), encoding="utf-8")
        runtime.stash(repo, spec_path, store / f"batch-{offset // 16}")
    return [str(item["root"]) for item in specs]


def restore_image_dependencies(repo: Path, store: Path) -> None:
    for batch in sorted(store.glob("batch-*")):
        runtime.restore(repo, batch, store / f"{batch.name}-restored.json")
    shutil.rmtree(store)


def remove_runtime_paths(repo: Path, roots: list[str]) -> None:
    """Drop image runtime paths after execution and before candidate collection."""
    for name in roots:
        relative = runtime._relative_path(name)
        target = repo / relative
        if any(parent.is_symlink() for parent in target.parents if parent != repo and repo in parent.parents):
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def main() -> int:
    action, workspace, argument = sys.argv[1:]
    repo = Path(workspace)
    if action == "stash":
        print(json.dumps(stash_image_dependencies(repo, sys.stdin.read().strip(), Path(argument))))
    elif action == "restore":
        restore_image_dependencies(repo, Path(argument))
    elif action == "remove":
        remove_runtime_paths(repo, json.loads(sys.stdin.read()))
    else:
        raise ValueError("unknown runtime dependency action")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
