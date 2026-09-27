"""Installed workflows are supplied by the evaluation caller."""

from __future__ import annotations

import asyncio

import pytest

from opencollab_eval.workflow_loader import load_workflow


def test_loads_and_executes_an_external_workflow(tmp_path, monkeypatch):
    (tmp_path / "external_workflow.py").write_text(
        "async def solve(context, args):\n    return context + args['amount']\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    solve = load_workflow("external_workflow:solve")

    assert asyncio.run(solve(2, {"amount": 3})) == 5


@pytest.mark.parametrize("reference", ["solve", "", "module:", ":solve", "module:solve:extra"])
def test_rejects_an_ambiguous_workflow_entry(reference):
    with pytest.raises(ValueError, match="module:function"):
        load_workflow(reference)


@pytest.mark.parametrize("reference", ["missing_workflow:solve", "json:missing", "json:__name__"])
def test_reports_unavailable_or_noncallable_entries(reference):
    with pytest.raises(ValueError, match="workflow"):
        load_workflow(reference)
