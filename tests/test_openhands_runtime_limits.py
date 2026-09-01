from __future__ import annotations

import math

import pytest

from opencollab_eval.generation.openhands_runtime import _terminal_action_timeout


@pytest.mark.parametrize("value", [True, False, -1, math.nan, math.inf, "oops"])
def test_terminal_action_timeout_rejects_invalid_model_values(value) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        _terminal_action_timeout(value)


@pytest.mark.parametrize("value, expected", [(None, 30.0), (0, 30.0), ("2.5", 2.5)])
def test_terminal_action_timeout_keeps_default_and_valid_values(value, expected) -> None:
    assert _terminal_action_timeout(value) == expected
