from __future__ import annotations

import asyncio
from typing import Any

from opencollab_eval.workflows._public_api import format_findings_report
from opencollab_eval.workflows.analyst_solve import analyst_solve

ENFORCEMENT_OFF = "off"
ENFORCEMENT_ON = "needs-enforcement"


def _recon_fn():
    return analyst_solve.__globals__["_recon"]


def _cited(summary="root cause located", anchor="fs.py:42"):
    return {
        "findings": [
            {
                "aspect": "bug",
                "claim": "off-by-one",
                "evidence_anchor": anchor,
                "verified": True,
                "confidence": "high",
            }
        ],
        "summary": summary,
        "insufficient_evidence": False,
    }


def test_local_findings_renderer_preserves_evidence_labels() -> None:
    rendered = format_findings_report(_cited())

    assert "Summary: root cause located" in rendered
    assert "(bug) off-by-one [fs.py:42] — verified, confidence=high" in rendered


class _ReconCtx:
    def __init__(self, *, workspace_root=None, with_drafts=True):
        self.workspace_root = workspace_root
        self.agent_calls: list[dict[str, Any]] = []
        self.draft_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        if with_drafts:
            self.draft_findings = self._draft_findings

    def tokens_remaining(self) -> float:
        return 1_000_000.0

    def tokens_spent(self) -> int:
        return 0

    async def _draft_findings(self, prompt, *, label=None, budget=None):
        self.draft_calls.append({"prompt": prompt, "label": label, "budget": budget})
        return _cited(summary=f"draft for {label}")

    async def agent(self, prompt, **kwargs):
        self.agent_calls.append({"prompt": prompt, **kwargs})
        return f"refined:{kwargs.get('label')}"

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def log(self, message):
        self.logs.append(message)


_DIMS = [
    {"aspect": "origin", "question": "where?", "hints": ["pkg/t.py"]},
    {"aspect": "contract", "question": "callers?", "hints": ["grep callers"]},
]


def _trivial_repo(tmp_path) -> tuple[str, str]:
    (tmp_path / "mod.py").write_text(
        'def tiny(a, b):\n    """Add a and b together for the widget subsystem."""\n'
        "    # TODO: implement this function\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    goal = (
        "TASK: Implement the function `tiny`.\n"
        f"- The function stub is at: {tmp_path / 'mod.py'} (near line 1)\n"
    )
    return str(tmp_path), goal


def _scout_calls(ctx: _ReconCtx):
    return [call for call in ctx.agent_calls if str(call.get("label", "")).startswith("scout:")]


def test_recon_commit_first_seeds_drafts_and_plumbs_fallback(tmp_path):
    ctx = _ReconCtx(workspace_root=str(tmp_path), with_drafts=True)
    root, goal = _trivial_repo(tmp_path)
    ctx.workspace_root = root

    asyncio.run(_recon_fn()(ctx, goal, _DIMS, ENFORCEMENT_ON))

    scouts = _scout_calls(ctx)
    assert len(ctx.draft_calls) == len(scouts)
    assert all(call["label"].endswith(":draft") for call in ctx.draft_calls)
    for call in scouts:
        assert "committed draft" in call["prompt"]
        assert "submit_findings" in call["prompt"]
        assert isinstance(call["harvest_fallback"], str) and call["harvest_fallback"].strip()
        assert "draft for" in call["harvest_fallback"]


def test_recon_off_makes_no_draft_call(tmp_path):
    root, goal = _trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, with_drafts=True)

    asyncio.run(_recon_fn()(ctx, goal, _DIMS, ENFORCEMENT_OFF))

    assert ctx.draft_calls == []
    scouts = _scout_calls(ctx)
    assert len(scouts) == len(_DIMS)
    assert all(call.get("harvest_fallback") is None for call in scouts)
    assert all("committed draft" not in call["prompt"] for call in scouts)
    assert all("Pre-recon fact sheet" not in call["prompt"] for call in scouts)


def test_recon_on_without_draft_findings_degrades_gracefully(tmp_path):
    root, goal = _trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, with_drafts=False)

    asyncio.run(_recon_fn()(ctx, goal, _DIMS, ENFORCEMENT_ON))

    scouts = _scout_calls(ctx)
    assert ctx.draft_calls == []
    assert all(call.get("harvest_fallback") is None for call in scouts)
    assert all("committed draft" not in call["prompt"] for call in scouts)
    assert any("Pre-recon fact sheet" in call["prompt"] for call in scouts)
