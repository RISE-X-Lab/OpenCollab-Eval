"""Prepare runner ownership and runtime checks for focused runner tests."""

from __future__ import annotations

import pytest

import opencollab_eval.commands.swe_v1_prolite_runner as runner


def _clear_preexisting_runner(monkeypatch):
    monkeypatch.setattr(
        runner._controller,
        "recover_existing_remote_summary",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        runner._controller,
        "probe_preexisting_remote_execution",
        lambda **kwargs: None,
    )


@pytest.fixture(autouse=True)
def verified_remote_runtime(monkeypatch):
    monkeypatch.setattr(
        runner,
        "verify_remote_runtime",
        lambda **kwargs: {"sha256": "a" * 64},
    )
    _clear_preexisting_runner(monkeypatch)


@pytest.fixture(autouse=True)
def no_existing_remote_runner(monkeypatch):
    _clear_preexisting_runner(monkeypatch)
