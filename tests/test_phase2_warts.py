"""Phase 2 — cheap behavioral warts (enforcement-gated; OFF == reference).

* 2A (post-brake dead turn) — the FIRST post-brake turn narrows the toolset to
  submit-only (or {submit_findings, request_extension} when the extension valve is
  armed). The nudge injected AT that trip now states the narrowed toolset up front,
  so a model does not reflexively re-issue a now-removed exploration tool and burn
  the turn on an "unknown tool" error before the provider-compat retry recovers it.

* 2B (redundant final tester) — when every phase already passed its own adversarial
  tester on the current tree and no forced write touched it since, the whole-goal
  ``tester:final`` would re-run near-identical checks on a byte-identical tree.
  Behind enforcement it is skipped; with enforcement OFF it runs exactly as the
  reference, and it always runs when a phase failed/blocked (repair loop intact).
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab_eval.workflows.analyst_solve import analyst_solve

F2P = ["tests/test_widget.py::test_empty"]
ON = "needs-enforcement"


# --------------------------------------------------------------------------- #
# 2B — skip the redundant whole-goal final tester (enforcement-gated).
# --------------------------------------------------------------------------- #
class ScriptedCtx:
    """WorkflowContext stand-in scripting agent() replies (mirrors the f2p-gate
    test's fake). ``tree``/``source`` answer the working-tree probes."""

    def __init__(self, replies: list[Any], *, tree: bool | None = True) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.budget = _FakeBudget()
        self._tree = tree

    async def agent(self, prompt, *, schema=None, label=None, tools=None, **kw):
        self.agent_calls.append({"prompt": prompt, "label": label, "schema": schema, **kw})
        reply = self._replies.pop(0) if self._replies else None
        if isinstance(reply, dict) and tools:
            for tool in tools:
                if getattr(tool, "name", "") == "run_tests":
                    tool._verified_targets.update(reply.get("tests_run") or ())
        return reply

    async def parallel(self, thunks):
        return [await t() for t in thunks]

    async def phase(self, title):
        pass

    async def log(self, message):
        self.logs.append(message)

    async def tree_changed(self):
        return self._tree

    async def source_changed(self, exclude_paths=()) -> bool | None:
        return self._tree


class _FakeBudget:
    total = None

    def remaining(self) -> float:
        return float("inf")

    def spent(self) -> int:
        return 0


DIMS = {"dimensions": [{"aspect": "bug", "question": "where?", "hints": []}]}
PLAN = {
    "root_cause": "rc",
    "approach": "ap",
    "phases": [{"goal": "g", "files": ["f.py"], "done": "behaves"}],
}


def _pass(*, tests_run=F2P, failed_count=0) -> dict[str, Any]:
    return {"verdict": "PASS", "findings": "", "tests_run": list(tests_run), "failed_count": failed_count}


def _wf_fn():
    return analyst_solve


def _labels(ctx) -> list[str]:
    return [c["label"] or "" for c in ctx.agent_calls]


def test_2b_skips_redundant_final_tester_when_enforced_and_all_passed():
    # Enforcement ON, the single phase passes its tester (and the f2p gate). The
    # final tester is redundant on the unchanged tree -> skipped. Note there is NO
    # final-verify reply scripted: if the final tester ran it would (wrongly) consume
    # one. The run still completes "done" via passed_phases.
    ctx = ScriptedCtx(
        replies=[DIMS, "scout", PLAN, "coded", _pass()],  # no final-verify reply
        tree=True,
    )
    result = asyncio.run(
        _wf_fn()(ctx, {"description": "fix the widget", "fail_to_pass": F2P, "enforcement_strength": ON})
    )

    assert "tester:final" not in _labels(ctx)  # the final tester never ran
    assert result["phases"][0]["status"] == "passed"
    assert result["status"] == "done"
    assert result.get("final_verify_skipped") is True
    assert result["final_verdict"] is None
    assert any("skipping redundant final tester" in m for m in ctx.logs)


def test_2b_final_tester_runs_when_enforcement_off():
    # OFF == reference: the same all-passed scenario runs the final tester exactly as
    # before (it consumes the scripted final-verify reply) and sets no skip flag.
    ctx = ScriptedCtx(
        replies=[DIMS, "scout", PLAN, "coded", _pass(), _pass()],  # incl. final verify
        tree=True,
    )
    result = asyncio.run(
        _wf_fn()(ctx, {"description": "fix the widget", "fail_to_pass": F2P})  # enforcement defaults off
    )

    assert "tester:final" in _labels(ctx)
    assert "final_verify_skipped" not in result
    assert result["status"] == "done"


def test_2b_final_tester_runs_when_a_phase_did_not_pass_even_enforced():
    # Repair loop intact: enforcement ON but the phase is BLOCKED (not "passed"), so
    # the skip is conservatively declined and the whole-goal final tester still runs.
    blocked = {"verdict": "BLOCKED", "findings": "environmental blocker", "tests_run": [], "failed_count": 0}
    ctx = ScriptedCtx(
        replies=[DIMS, "scout", PLAN, "coded", blocked, _pass()],  # final verify present
        tree=True,
    )
    result = asyncio.run(
        _wf_fn()(ctx, {"description": "fix the widget", "fail_to_pass": F2P, "enforcement_strength": ON})
    )

    assert result["phases"][0]["status"] == "blocked"
    assert "tester:final" in _labels(ctx)  # not skipped — a phase did not pass
    assert "final_verify_skipped" not in result
