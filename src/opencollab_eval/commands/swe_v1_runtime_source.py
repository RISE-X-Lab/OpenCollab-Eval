"""Resolve the OpenCollab source tree included in a remote evaluation runtime."""

from __future__ import annotations

import ast
import importlib
import os
import re
from collections.abc import Callable, Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

MIN_OPENCOLLAB_RELEASE = (0, 4, 1)


def declared_opencollab_version(package_root: Path) -> str | None:
    """Read a literal ``__version__`` assignment from an OpenCollab checkout."""
    init_path = package_root / "__init__.py"
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise RuntimeError("the selected OpenCollab source version cannot be read") from exc
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def runtime_directory_sources(
    *,
    sync_dirs: Iterable[str],
    runtime_input_path: Callable[[str], Path],
    verify_import_contract: Callable[[], None],
    distribution_version_resolver: Callable[[str], str] = version,
) -> tuple[dict[str, Path], str]:
    """Select synchronized runtime directories and their OpenCollab version."""
    sources = {relative: runtime_input_path(relative) for relative in sync_dirs}
    package = importlib.import_module("opencollab")
    configured_root = os.environ.get("OPENCOLLAB_SOURCE_ROOT")
    if configured_root:
        source_root = Path(configured_root).expanduser().resolve(strict=True)
        package_root = source_root / "opencollab"
        if not (source_root / "pyproject.toml").is_file() or not package_root.is_dir():
            raise RuntimeError(
                "OPENCOLLAB_SOURCE_ROOT must identify an OpenCollab source checkout"
            )
        sources["src/opencollab"] = package_root
        distribution_version = declared_opencollab_version(package_root)
        if distribution_version is None:
            raise RuntimeError("the selected OpenCollab source version is missing")
    else:
        sources["src/opencollab"] = Path(next(iter(package.__path__))).resolve()
        try:
            distribution_version = distribution_version_resolver("opencollab")
        except PackageNotFoundError as exc:
            raise RuntimeError("the OpenCollab distribution metadata is missing") from exc
    release_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", distribution_version)
    release = tuple(map(int, release_match.groups())) if release_match else ()
    if release < MIN_OPENCOLLAB_RELEASE or release >= (0, 6, 0):
        raise RuntimeError(
            f"OpenCollab >=0.4.1,<0.6 is required, found {distribution_version}"
        )
    if not configured_root and getattr(package, "__version__", None) != distribution_version:
        raise RuntimeError(
            "the imported OpenCollab source version does not match its distribution metadata"
        )
    verify_import_contract()
    return sources, distribution_version


__all__ = ["declared_opencollab_version", "runtime_directory_sources"]
