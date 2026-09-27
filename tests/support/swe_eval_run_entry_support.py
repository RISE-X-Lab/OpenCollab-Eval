"""Shared preparation for swe eval run entry."""

from __future__ import annotations

import importlib
from typing import Any


def _load_entry_module() -> Any:
    module = importlib.import_module("opencollab_eval.commands.swe_eval_run")
    return importlib.reload(module)

