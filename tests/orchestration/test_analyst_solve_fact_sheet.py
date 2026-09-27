from __future__ import annotations

import asyncio
from typing import Any

from opencollab_eval.workflows.analyst_solve import analyst_solve


def _recon_fn():
    return analyst_solve.__globals__["_recon"]


class _ReconCtx:
    def __init__(self, *, workspace_root=None, remaining=1_000_000) -> None:
        self.workspace_root = workspace_root
        self.agent_calls: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self._remaining = remaining

    def tokens_remaining(self) -> float:
        return float(self._remaining)

    def tokens_spent(self) -> int:
        return 0

    async def agent(self, prompt, **kwargs):
        self.agent_calls.append({"prompt": prompt, **kwargs})
        return f"findings for {kwargs.get('label')}"

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def log(self, message):
        self.logs.append(message)


_THREE_DIMS = [
    {"aspect": "origin", "question": "where?", "hints": ["look in pkg/target.py"]},
    {"aspect": "contract", "question": "callers?", "hints": ["grep callers"]},
    {"aspect": "edges", "question": "edge cases?", "hints": ["the docstring"]},
]


def _build_trivial_repo(root) -> tuple[str, str]:
    (root / "mod.py").write_text(
        "def tiny(x):\n    # TODO: implement this function\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    goal = (
        "TASK: Implement the function `tiny`.\n"
        f"- The function stub is at: {root / 'mod.py'} (near line 1)\n"
    )
    return str(root), goal


def _scout_calls(ctx: _ReconCtx) -> list[dict[str, Any]]:
    return [call for call in ctx.agent_calls if str(call.get("label", "")).startswith("scout:")]


def test_recon_off_is_reference(tmp_path):
    ctx = _ReconCtx(workspace_root=str(tmp_path), remaining=1_000_000)

    asyncio.run(
        _recon_fn()(ctx, "TASK: Implement the function `compute_widget`.", _THREE_DIMS, "off")
    )

    scouts = _scout_calls(ctx)
    assert len(scouts) == 3
    expected_cap = min(250_000, (1_000_000 - 600_000) // 3)
    assert all(call["budget"] == expected_cap for call in scouts)
    assert all("Pre-recon fact sheet" not in call["prompt"] for call in scouts)
    assert any("look in pkg/target.py" in call["prompt"] for call in scouts)
    assert any("grep callers" in call["prompt"] for call in scouts)


def test_recon_on_trivial_trims_scouts_and_injects_fact_sheet(tmp_path):
    root, goal = _build_trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, remaining=1_000_000)

    asyncio.run(_recon_fn()(ctx, goal, _THREE_DIMS, "needs-enforcement"))

    scouts = _scout_calls(ctx)
    assert len(scouts) == 1
    base = min(250_000, (1_000_000 - 600_000) // 1)
    assert scouts[0]["budget"] == max(1, int(base * 0.45))
    assert "Pre-recon fact sheet" in scouts[0]["prompt"]
    assert scouts[0]["enforcement_strength"] == "needs-enforcement"


def test_recon_on_ample_budget_runs_full_fanout_at_full_depth(tmp_path):
    root, goal = _build_trivial_repo(tmp_path)
    ctx = _ReconCtx(workspace_root=root, remaining=2_000_000)

    asyncio.run(_recon_fn()(ctx, goal, _THREE_DIMS, "needs-enforcement"))

    scouts = _scout_calls(ctx)
    assert len(scouts) == 3
    expected_cap = min(250_000, (2_000_000 - 600_000) // 3)
    assert all(call["budget"] == expected_cap for call in scouts)
    assert all("Pre-recon fact sheet" in call["prompt"] for call in scouts)
    assert all(call["enforcement_strength"] == "needs-enforcement" for call in scouts)
    assert any("pool ample" in message for message in ctx.logs)
