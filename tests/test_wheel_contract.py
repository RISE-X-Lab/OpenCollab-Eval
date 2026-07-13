from __future__ import annotations

import os
from pathlib import Path

import opencollab
from opencollab.sdk import SDK_API_VERSION


def test_opencollab_sdk_can_come_from_the_built_wheel() -> None:
    expected_root = os.environ.get("OPENCOLLAB_EXPECTED_WHEEL_ROOT")
    if expected_root:
        assert Path(opencollab.__file__).is_relative_to(Path(expected_root))
    assert SDK_API_VERSION == 1

