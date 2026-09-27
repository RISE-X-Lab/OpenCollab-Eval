from __future__ import annotations

import pytest

from opencollab_eval.workflows._validation_council_solve_defs import (
    MAX_CODER_ROUNDS_ENV,
    ROLE_BUDGET_ENV,
    max_coder_rounds,
    role_budget,
)


def test_role_budget_reads_current_environment_and_preserves_legacy_alias(monkeypatch):
    monkeypatch.delenv(ROLE_BUDGET_ENV, raising=False)
    monkeypatch.delenv("OPENCOLLAB_G11_ROLE_BUDGET", raising=False)
    assert role_budget(123) == 123
    monkeypatch.setenv("OPENCOLLAB_G11_ROLE_BUDGET", "456")
    assert role_budget(123) == 456
    monkeypatch.setenv(ROLE_BUDGET_ENV, "unbounded")
    assert role_budget(123) is None
    monkeypatch.setenv(ROLE_BUDGET_ENV, "789")
    assert role_budget(123) == 789


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad"])
def test_invalid_role_configuration_is_rejected(monkeypatch, value):
    monkeypatch.setenv(ROLE_BUDGET_ENV, value)
    with pytest.raises(ValueError, match="positive integer"):
        role_budget(123)
    monkeypatch.setenv(MAX_CODER_ROUNDS_ENV, value)
    with pytest.raises(ValueError, match="positive integer"):
        max_coder_rounds()


def test_repair_round_configuration_keeps_default_and_honors_override(monkeypatch):
    monkeypatch.delenv(MAX_CODER_ROUNDS_ENV, raising=False)
    assert max_coder_rounds() == 3
    monkeypatch.setenv(MAX_CODER_ROUNDS_ENV, "6")
    assert max_coder_rounds() == 6


def test_provider_recovery_window_is_separate_from_normal_model_call_timeout(monkeypatch):
    from opencollab_eval.workflows._validation_council_solve_defs import structured_role_timeout_seconds

    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "600")
    monkeypatch.setenv("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET", "3600")
    assert structured_role_timeout_seconds() == 4260
    assert __import__("os").environ["OPENCOLLAB_LLM_TIMEOUT"] == "600"
    monkeypatch.setenv("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET", "nan")
    with pytest.raises(ValueError, match="finite and non-negative"):
        structured_role_timeout_seconds()
