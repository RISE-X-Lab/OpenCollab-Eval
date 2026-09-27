"""Load a caller-selected workflow without owning a strategy registry."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any


def load_workflow(reference: str) -> Callable[..., Any]:
    """Resolve an installed ``module:function`` entry supplied by the caller."""
    module_name, separator, attribute = reference.strip().partition(":")
    if not separator or not module_name or not attribute or ":" in attribute:
        raise ValueError("workflow must use module:function form")
    try:
        module = import_module(module_name)
        workflow = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot load workflow {reference!r}: {exc}") from exc
    if not callable(workflow):
        raise ValueError(f"workflow {reference!r} must be callable")
    return workflow


__all__ = ["load_workflow"]
