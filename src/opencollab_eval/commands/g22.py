"""Compatibility command for running Duo through the official evaluator."""

from __future__ import annotations

from collections.abc import Sequence

from . import duo
from .duo import DEFAULT_WORKFLOW_ENV, WORKFLOW, parallel, resolve_config


def main(argv: Sequence[str] | None = None) -> int:
    return duo.main(argv, prog="oc-eval g22")


__all__ = ["DEFAULT_WORKFLOW_ENV", "WORKFLOW", "main", "parallel", "resolve_config"]


if __name__ == "__main__":
    raise SystemExit(main())
