"""Assemble image dependencies through OC's public command_prefix option."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _common_directory(root):
    value = Path(_git(root, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else Path(root) / value).resolve()


def prepare(source, roots, store, candidate_count=2):
    """Capture already-selected public image roots before workflow execution."""
    source, store = Path(source).resolve(), Path(store).resolve()
    (store / "seed" / "roots").mkdir(parents=True)
    (store / "state").mkdir()
    names = []
    for name in roots:
        relative = PurePosixPath(name)
        if not relative.parts or relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("dependency root must be a repository-relative path")
        item = source / name
        if item.is_symlink() or not item.exists():
            raise ValueError("dependency root must retain its image-provided contents")
        target = store / "seed" / "roots" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target)
        names.append(name)
    config = {"source_workspace": str(source), "roots": names, "git_common_dir": str(_common_directory(source))}
    (store / "seed" / "config.json").write_text(json.dumps(config))
    (store / "ready").mkdir()
    (store / "claims").mkdir()
    # Copies and symlink traversal finish before the workflow starts.
    for index in range(candidate_count):
        ready = store / "ready" / str(index)
        shutil.copytree(store / "seed" / "roots", ready, symlinks=True)
        _prepare_links(ready, source)
    return config


def _prepare_links(roots, source):
    for link in roots.rglob("*"):
        if not link.is_symlink():
            continue
        value = os.readlink(link)
        if not os.path.isabs(value):
            continue
        try:
            target = Path(value).relative_to(source)
        except ValueError:
            continue
        parent = link.relative_to(roots).parent
        link.unlink()
        link.symlink_to(os.path.relpath(target, parent))


def hydrate(store, workspace):
    """Move an already prepared copy on the candidate's first command."""
    started = time.monotonic()
    store, workspace = Path(store).resolve(), Path(workspace).resolve()
    config = json.loads((store / "seed" / "config.json").read_text())
    source = Path(config["source_workspace"])
    if workspace == source:
        return False
    common = _common_directory(workspace)
    if common != Path(config["git_common_dir"]).resolve() or not (workspace / ".git").is_file():
        raise ValueError("candidate must be a linked worktree of the source repository")
    marker = store / "state" / (workspace.name + ".json")
    with (store / "state" / (workspace.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            return False
        # The dependency store can be on the image filesystem while linked
        # candidate worktrees live on a separate /tmp mount.
        for name in config["roots"]:
            target = workspace / name
            if target.exists() or target.is_symlink():
                raise ValueError("candidate dependency root already exists before preparation")
        claimed = store / "claims" / workspace.name
        with (store / "state" / "allocation.lock").open("a") as allocation:
            fcntl.flock(allocation, fcntl.LOCK_EX)
            available = sorted((store / "ready").iterdir())
            if claimed.exists() or not available:
                raise ValueError("candidate dependency preparation is incomplete or exhausted")
            allocated = available[0]
            allocated.rename(claimed)
        installed = []
        try:
            for name in config["roots"]:
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                cached = claimed / name
                try:
                    cached.rename(target)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    # An ignored directory becomes an untracked Git path when
                    # replaced by a symlink. Materialize it on the worktree
                    # mount, then publish it with a same-filesystem rename.
                    with tempfile.TemporaryDirectory(prefix=".opencollab-hydrate-", dir=target.parent) as temp:
                        staged = Path(temp) / target.name
                        if cached.is_dir() and not cached.is_symlink():
                            shutil.copytree(cached, staged, symlinks=True)
                        elif cached.is_symlink():
                            staged.symlink_to(os.readlink(cached))
                        else:
                            shutil.copy2(cached, staged)
                        staged.rename(target)
                    installed.append((target, cached, False))
                else:
                    installed.append((target, cached, True))
            marker.write_text(
                json.dumps(
                    {"workspace": str(workspace), "roots": config["roots"],
                     "elapsed_seconds": time.monotonic() - started}
                )
            )
        except BaseException:
            marker.unlink(missing_ok=True)
            for target, cached, moved in reversed(installed):
                if moved:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    target.rename(cached)
                else:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
            # Return only a fully rolled-back copy while allocation is locked.
            # The workspace lock still excludes a second writer for this candidate.
            with (store / "state" / "allocation.lock").open("a") as allocation:
                fcntl.flock(allocation, fcntl.LOCK_EX)
                claimed.rename(allocated)
            raise
    return True


def command_prefix(store, source, *, python="python3", helper=None, activation=""):
    """Return the callable accepted by attach_container and inherited by A/B."""
    helper = str(helper or Path(__file__).resolve())
    marker = shlex.quote(str(Path(store) / "state")) + '/"${PWD##*/}.json"'
    setup = (
        f'if [ "$PWD" != {shlex.quote(str(source))} ] && [ ! -f {marker} ]; then\n'
        f'  {shlex.quote(python)} {shlex.quote(helper)} hydrate {shlex.quote(str(store))} "$PWD" || exit $?\n'
        "fi\n"
    )

    def wrap(command):
        return setup + (activation + "\n" if activation else "") + command

    return wrap


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare(sys.argv[2], json.loads(sys.stdin.read()), sys.argv[3])
    elif sys.argv[1] == "hydrate":
        hydrate(sys.argv[2], sys.argv[3])
    else:
        raise ValueError("unknown dependency action")
