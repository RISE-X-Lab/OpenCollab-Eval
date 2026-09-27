"""Locate test assets, source-only checks, and installed package modules."""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.package_test_support import module_path

TEST_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", TEST_ROOT.parent)).expanduser().resolve()
PACKAGE_ROOT = module_path("opencollab_eval").parent.parent
