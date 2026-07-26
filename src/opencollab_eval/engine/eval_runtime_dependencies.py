"""Preserve image-provided test runners while rebuilding a clean eval tree."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

SCHEMA = "opencollab.eval_runtime_dependencies.v1"
SPEC_FIELDS = {"root", "required_paths", "kind", "candidate_protected"}


def _relative_path(value: object) -> PurePosixPath:
    path = PurePosixPath(str(value or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError("runtime dependency path must be a safe relative path")
    return path


def _load_specs(path: Path) -> list[dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("runtime dependency spec must be a bounded list")
    specs = []
    for item in value:
        if not isinstance(item, dict) or set(item) != SPEC_FIELDS:
            raise ValueError("runtime dependency spec has invalid fields")
        root = _relative_path(item["root"])
        required = item["required_paths"]
        kind = item["kind"]
        candidate_protected = item["candidate_protected"]
        if not isinstance(required, list) or not required or len(required) > 16:
            raise ValueError("runtime dependency required paths must be a bounded list")
        if kind not in {"directory", "file"} or not isinstance(candidate_protected, bool):
            raise ValueError("runtime dependency kind or protection is invalid")
        paths = [_relative_path(value) for value in required]
        if any(path != root and root not in path.parents for path in paths):
            raise ValueError("runtime requirement must be inside its dependency root")
        specs.append(
            {
                "root": str(root),
                "required_paths": [str(value) for value in paths],
                "kind": kind,
                "candidate_protected": candidate_protected,
            }
        )
    return specs


def _spec_sha256(specs: list[dict[str, object]]) -> str:
    payload = json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ignored(repo: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--", relative],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _move_tree(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(source, target, symlinks=True)
        shutil.rmtree(source)


def _move_file(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(source, target)
        source.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def stash(repo: Path, spec_path: Path, store: Path) -> dict[str, object]:
    specs = _load_specs(spec_path)
    shutil.rmtree(store, ignore_errors=True)
    store.mkdir(mode=0o700)
    grouped: dict[str, tuple[list[str], str, bool]] = {}
    for item in specs:
        existing = grouped.get(item["root"])
        if existing and (
            existing[1] != item["kind"] or existing[2] != item["candidate_protected"]
        ):
            raise ValueError("runtime dependency root has conflicting declarations")
        grouped.setdefault(
            item["root"],
            ([], item["kind"], item["candidate_protected"]),
        )[0].extend(item["required_paths"])
    entries = []
    for index, (root_name, declaration) in enumerate(grouped.items()):
        required_paths, kind, candidate_protected = declaration
        root = repo / root_name
        present = [
            name
            for name in required_paths
            if (repo / name).exists()
            and (name == root_name or os.access(repo / name, os.X_OK))
        ]
        if not present:
            continue
        if root.is_symlink():
            raise ValueError("local test runner dependency root lacks trusted image provenance")
        ignored = _ignored(repo, root_name)
        if kind == "directory" and (not root.is_dir() or not ignored):
            raise ValueError("local test runner dependency root lacks trusted image provenance")
        if kind == "file" and root.is_dir():
            raise ValueError("runtime dependency file root is not a regular file")
        if kind == "file" and root.is_file() and not ignored:
            continue
        stored = store / f"root-{index:02d}"
        content_sha256 = ""
        if kind == "directory":
            resolved_root = root.resolve(strict=True)
            if any(
                not _within_root((repo / name).resolve(strict=True), resolved_root)
                for name in present
            ):
                raise ValueError("local test runner escapes its dependency root")
            _move_tree(root, stored)
        elif root.is_file() and present == [root_name]:
            content_sha256 = _file_sha256(root)
            _move_file(root, stored)
        else:
            raise ValueError("local test runner dependency root has an unsupported type")
        entries.append(
            {
                "root": root_name,
                "stored": stored.name,
                "required_paths": present,
                "kind": kind,
                "candidate_protected": candidate_protected,
                "content_sha256": content_sha256,
            }
        )
    manifest = {
        "schema": SCHEMA,
        "phase": "stashed",
        "spec_sha256": _spec_sha256(specs),
        "entries": entries,
    }
    (store / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def restore(repo: Path, store: Path, output: Path) -> dict[str, object]:
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("phase") != "stashed":
        raise ValueError("runtime dependency manifest is invalid")
    restored = []
    for item in manifest.get("entries") or []:
        root_name = str(_relative_path(item.get("root")))
        target = repo / root_name
        stored = store / str(_relative_path(item.get("stored")))
        kind = item.get("kind")
        candidate_protected = item.get("candidate_protected")
        content_sha256 = item.get("content_sha256")
        stored_type_valid = stored.is_dir() if kind == "directory" else stored.is_file()
        if (
            kind not in {"directory", "file"}
            or not isinstance(candidate_protected, bool)
            or not isinstance(content_sha256, str)
            or (kind == "directory" and content_sha256)
            or (kind == "file" and re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None)
            or target.exists()
            or target.is_symlink()
            or stored.is_symlink()
            or not stored_type_valid
        ):
            raise ValueError("runtime dependency restore target is not empty")
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "directory":
            _move_tree(stored, target)
        else:
            _move_file(stored, target)
            if _file_sha256(target) != content_sha256:
                raise ValueError("restored runtime dependency content identity changed")
        required_paths = [
            str(_relative_path(value)) for value in item.get("required_paths") or []
        ]
        if not required_paths or any(
            not (repo / name).exists()
            or name != root_name
            and not os.access(repo / name, os.X_OK)
            for name in required_paths
        ):
            raise ValueError("restored runtime dependency lost its test runner")
        restored.append(
            {
                "root": root_name,
                "required_paths": required_paths,
                "kind": kind,
                "candidate_protected": candidate_protected,
                "content_sha256": content_sha256,
            }
        )
    report = {
        "schema": SCHEMA,
        "phase": "restored",
        "source": "pinned_image_runtime_with_trusted_public_preparation",
        "solver_visible": False,
        "spec_sha256": manifest.get("spec_sha256"),
        "entries": restored,
    }
    output.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 4 and args[0] == "stash":
            stash(Path(args[1]), Path(args[2]), Path(args[3]))
        elif len(args) == 4 and args[0] == "restore":
            restore(Path(args[1]), Path(args[2]), Path(args[3]))
        else:
            raise ValueError("usage: eval_runtime_dependencies.py stash REPO SPEC STORE | restore REPO STORE OUTPUT")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"runtime dependency preservation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
