"""Helpers for locating installed OpenCollab-Eval modules and resources."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def module_path(module_name: str) -> Path:
    """Return the regular source path backing an importable module."""

    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise ImportError(f"cannot locate module {module_name!r}")
    path = Path(spec.origin).resolve()
    if not path.is_file():
        raise ImportError(f"module {module_name!r} has no regular source file")
    return path


def resource_path(name: str) -> Path:
    """Return a packaged shell resource by name."""

    path = module_path("opencollab_eval.resources").with_name(name)
    if not path.is_file():
        raise ImportError(f"cannot locate packaged resource {name!r}")
    return path
