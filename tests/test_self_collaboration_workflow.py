"""Tests for ``opencollab_eval.workflows.self_collaboration``.

The workflow's reason to exist is that it is the code-sequenced twin of
``OpenCollab/configs/team.handoff.experiment.yaml``: same three roles, same six
directed edges, same tool bundles, and only the sequencing differs. That claim
is worth nothing as a comment, so the parity is read off both sides here and
compared. Everything else is pure orchestration over ``ctx.agent``, so a
scripted context stand-in exercises it without touching an LLM.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from opencollab_eval.generation.gen_prediction_constants import WORKING_TOOL_NAMES

_module = importlib.import_module("opencollab_eval.workflows.self_collaboration")
self_collaboration = _module.self_collaboration
self_collaboration_reading_analyst = _module.self_collaboration_reading_analyst
DECLARED_EDGES = _module.DECLARED_EDGES

# The coordination tools are scheduler-owned and have no referent in a
# workflow, so they are the one thing the twin is allowed to drop.
COORDINATION_TOOLS = {"message_agent", "team_status"}


def _team_config() -> dict[str, Any]:
    """The team this workflow mirrors, read from the OpenCollab working tree.

    Reachable when OpenCollab is installed from source, which is how the
    experiment runs; a wheel carries the package without ``configs/``, and
    there the parity tests skip rather than fail on a missing file.
    """
    import opencollab

    path = (
        Path(opencollab.__file__).resolve().parent.parent
        / "configs"
        / "team.handoff.experiment.yaml"
    )
    if not path.is_file():
        pytest.skip(f"team config not reachable from this install: {path}")
    return yaml.safe_load(path.read_text())


def _role_tools(module: Any, role: str) -> set[str]:
    builder = getattr(module, f"_{role}_tools")
    return {tool.name for tool in builder()}


def test_the_twin_walks_exactly_the_edges_the_team_declares():
    topology = _team_config()["topology"]
    declared = {
        f"{sender}->{receiver}"
        for sender, receivers in topology.items()
        for receiver in receivers
    }
    assert set(DECLARED_EDGES) == declared
    assert len(DECLARED_EDGES) == len(declared) == 6


@pytest.mark.parametrize("role", ["analyst", "coder", "tester"])
def test_each_role_holds_the_team_bundle_minus_the_coordination_tools(role):
    team_tools = set(_team_config()["roles"][role]["tools"])
    assert _role_tools(_module, role) == team_tools - COORDINATION_TOOLS


def test_the_analyst_holds_the_single_agent_working_set():
    # Not a restatement of the parity test above: this is why the team gives
    # its analyst those tools in the first place. If the twin's analyst were
    # weaker than the arm it is compared against, a difference between them
    # could be read off the tool bundle instead of off the sequencing.
    assert _role_tools(_module, "analyst") == set(WORKING_TOOL_NAMES)


class ScriptedCtx:
    """WorkflowContext stand-in scripting agent() replies in order."""

    def __init__(
        self,
        replies: list[Any],
        *,
        source_changed: bool | None = True,
        source_changed_seq: list[bool | None] | None = None,
        diff: str | None = None,
        diffs: list[str | None] | None = None,
    ) -> None:
        self._replies = list(replies)
        self._source_changed = source_changed
        # The workflow probes the tree twice per round for different reasons --
        # once after the analyst, once after the coder -- so a test that cares
        # which agent wrote has to answer them differently.
        self._source_changed_seq = (
            None if source_changed_seq is None else list(source_changed_seq)
        )
        self.source_changed_calls = 0
        self.source_changed_excludes: list[list[str]] = []
        self._diff = diff
        self._diffs = None if diffs is None else list(diffs)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []

    def tokens_spent(self) -> int:
        return 42

    async def agent(
        self,
        prompt,
        *,
        schema=None,
        label=None,
        tools=None,
        isolation=False,
        budget=None,
    ):
        self.agent_calls.append(
            {
                "prompt": prompt,
                "label": label,
                "schema": schema,
                "budget": budget,
                "tools": tools,
            }
        )
        return self._replies.pop(0)

    async def diff(self):
        if self._diffs:
            return self._diffs.pop(0)
        return self._diff

    async def source_changed(self, exclude_paths=()):
        self.source_changed_excludes.append(list(exclude_paths))
        self.source_changed_calls += 1
        if self._source_changed_seq:
            return self._source_changed_seq.pop(0)
        return self._source_changed

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)


BRIEF = {
    "root_cause": "off-by-one in the pager",
    "files": ["pager.py"],
    "implementation_task": "clamp the upper bound in pager.page()",
    "verification_task": "the last page must render its final row",
}
CODER_OK = {
    "summary_for_tester": "clamped the bound in pager.page()",
    "report_for_analyst": "one file touched; the brief named the right one",
}
TESTER_PASS = {
    "verdict": "PASS",
    "findings_for_coder": "",
    "report_for_analyst": "test_pager passes and the row is present",
}
TESTER_FAIL = {
    "verdict": "FAIL",
    "findings_for_coder": "test_pager still errors at pager.py:41",
    "report_for_analyst": "the clamp is off by one in the other direction",
}
ACCEPT = {"decision": "ACCEPT", "note": "the tree answers the task"}


@pytest.mark.asyncio
async def test_a_round_that_passes_first_try_walks_five_of_the_six_edges():
    # tester -> coder carries findings, and a PASS has none. The edge is
    # unwalked because there was nothing to send, which is what should be
    # recorded: manufacturing a payload so that the count reaches six would
    # report a traversal the run did not need.
    ctx = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "done"
    assert set(result["edges_walked"]) == set(DECLARED_EDGES) - {"tester->coder"}
    assert result["edges_declared"] == list(DECLARED_EDGES)
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst",
        "coder:r1",
        "tester:r1",
        "analyst:adjudicate:r1",
    ]


@pytest.mark.asyncio
async def test_a_rejected_round_walks_all_six_declared_edges():
    revise = {
        "decision": "REVISE",
        "note": "the clamp is on the wrong side",
        "implementation_task": "clamp the lower bound instead",
        "verification_task": "check the first page as well",
    }
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_FAIL, revise, CODER_OK, TESTER_PASS, ACCEPT]
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert set(result["edges_walked"]) == set(DECLARED_EDGES)


@pytest.mark.asyncio
async def test_an_edge_the_analyst_leaves_empty_is_not_counted_as_walked():
    # The measured quantity is edges declared against edges walked, so a
    # payload that was never written has to read as an unwalked edge rather
    # than as one the script reached the line for.
    silent = dict(BRIEF, verification_task="")
    ctx = ScriptedCtx([silent, CODER_OK, TESTER_PASS, ACCEPT])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert "analyst->tester" not in result["edges_walked"]
    assert "analyst->coder" in result["edges_walked"]


@pytest.mark.asyncio
async def test_a_revision_reissues_the_analyst_instructions_and_carries_findings():
    revise = {
        "decision": "REVISE",
        "note": "the clamp is on the wrong side",
        "implementation_task": "clamp the lower bound instead",
        "verification_task": "check the first page as well",
    }
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_FAIL, revise, CODER_OK, TESTER_PASS, ACCEPT]
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "done"
    assert [r["decision"] for r in result["rounds"]] == ["REVISE", "ACCEPT"]
    second_coder = next(
        call for call in ctx.agent_calls if call["label"] == "coder:r2"
    )
    assert "clamp the lower bound instead" in second_coder["prompt"]
    assert "test_pager still errors at pager.py:41" in second_coder["prompt"]
    second_tester = next(
        call for call in ctx.agent_calls if call["label"] == "tester:r2"
    )
    assert "check the first page as well" in second_tester["prompt"]


@pytest.mark.asyncio
async def test_a_revision_without_new_instructions_keeps_the_standing_ones():
    empty_revise = {"decision": "REVISE", "note": "try again"}
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_FAIL, empty_revise, CODER_OK, TESTER_PASS, ACCEPT]
    )

    await self_collaboration(ctx, {"goal": "fix the pager"})

    second_coder = next(
        call for call in ctx.agent_calls if call["label"] == "coder:r2"
    )
    assert "clamp the upper bound in pager.page()" in second_coder["prompt"]


@pytest.mark.asyncio
async def test_an_analyst_that_returns_nothing_cannot_buy_another_round():
    # A dead adjudicator is not a decision to continue: falling through to
    # REVISE would let a silent failure spend a second coder round.
    ctx = ScriptedCtx([BRIEF, CODER_OK, TESTER_FAIL, None])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "stopped"
    assert len(result["rounds"]) == 1
    assert any("falling back to STOP" in line for line in ctx.logs)


@pytest.mark.asyncio
async def test_a_blocked_verdict_the_analyst_stops_on_is_reported_as_blocked():
    blocked = {
        "verdict": "BLOCKED",
        "findings_for_coder": "",
        "report_for_analyst": "no network; the dependency cannot be installed",
    }
    stop = {"decision": "STOP", "note": "environmental, not a code defect"}
    ctx = ScriptedCtx([BRIEF, CODER_OK, blocked, stop])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_the_analyst_is_told_when_the_coder_wrote_nothing():
    ctx = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT], source_changed=False)

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    adjudication = next(
        call for call in ctx.agent_calls if call["label"] == "analyst:adjudicate:r1"
    )
    assert "the source is unchanged" in adjudication["prompt"]
    assert result["rounds"][0]["source_changed"] is False


@pytest.mark.asyncio
async def test_a_dead_analyst_at_the_brief_stops_before_spending_a_coder():
    ctx = ScriptedCtx([None])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "error"
    assert result["edges_walked"] == []
    assert [call["label"] for call in ctx.agent_calls] == ["analyst"]


@pytest.mark.asyncio
async def test_the_analyst_writing_the_fix_itself_is_recorded_before_the_coder_runs():
    # The analyst holds the working bundle, so it can finish the task in the
    # analyze phase. Once the coder has run, a changed tree no longer names who
    # changed it -- which is why the probe has to happen while it still does.
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_PASS, ACCEPT],
        source_changed_seq=[True, True],
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["analyst_wrote_source"] is True
    assert result["rounds"][0]["source_changed"] is True


@pytest.mark.asyncio
async def test_a_tree_the_coder_changed_is_not_charged_to_the_analyst():
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_PASS, ACCEPT],
        source_changed_seq=[False, True],
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["analyst_wrote_source"] is False
    assert result["rounds"][0]["source_changed"] is True


@pytest.mark.asyncio
async def test_the_analyst_probe_runs_even_when_the_brief_is_unusable():
    # An analyst that wrote the fix and then returned nothing usable is the
    # worst case for this arm and the one most worth being able to name.
    ctx = ScriptedCtx(["not a brief"], source_changed_seq=[True])

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "error"
    assert result["analyst_wrote_source"] is True


class _Budget:
    def __init__(self, total: int) -> None:
        self.total = total


class BudgetedCtx(ScriptedCtx):
    """A ScriptedCtx that reports a pool and charges each call a fixed cost."""

    def __init__(
        self, replies, *, total: int, cost: int = 0, costs=None, **kwargs
    ) -> None:
        super().__init__(replies, **kwargs)
        self.budget = _Budget(total)
        self._cost = cost
        self._costs = None if costs is None else list(costs)
        self._spent = 0

    def tokens_spent(self) -> int:
        return self._spent

    async def agent(self, prompt, **kwargs):
        result = await super().agent(prompt, **kwargs)
        if self._costs:
            self._spent += self._costs.pop(0)
        else:
            self._spent += self._cost
        return result


@pytest.mark.asyncio
async def test_each_seat_is_capped_at_the_pool_divided_by_the_three_seats():
    # The team arm gives each of its three seats c * pool / N with c = 1.0. A
    # workflow draws from one pool, so the same rule has to be applied here or
    # the analyst -- which can spend without limit -- is not comparable to the
    # analyst it is being compared against.
    ctx = BudgetedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT], total=900, cost=100)

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["seat_cap"] == 300
    budgets = [call["budget"] for call in ctx.agent_calls]
    # analyst's first call sees a full seat; the coder and tester each see their
    # own untouched seat; the analyst's adjudication sees what it has left.
    assert budgets == [300, 300, 300, 200]
    assert result["seat_spend"] == {"analyst": 200, "coder": 100, "tester": 100}


@pytest.mark.asyncio
async def test_a_degenerate_run_records_the_same_seat_accounting_as_a_healthy_one():
    # The failure this arm actually hit in dw-subset50 was an analyst that
    # returned no brief. Without the seat keys the artifact cannot answer the
    # only question that failure raises -- how much of its 2M seat the analyst
    # still held -- and a degenerate run carries 7 keys where a healthy one
    # carries 12, so the two cannot be read side by side.
    ctx = BudgetedCtx([None], total=900, cost=250)

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "error"
    assert result["seat_cap"] == 300
    assert result["seat_spend"] == {"analyst": 250}
    healthy = await self_collaboration(
        BudgetedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT], total=900, cost=100),
        {"goal": "fix the pager"},
    )
    # Every key the degenerate return carries beyond its own "error" is a key
    # the completed return carries too, so the two rows line up.
    assert set(result) - {"error"} <= set(healthy)
    assert {"seat_cap", "seat_spend"} <= set(result)


@pytest.mark.asyncio
async def test_an_analyst_that_spends_its_whole_seat_analysing_still_leaves_the_coder_a_seat():
    ctx = BudgetedCtx([BRIEF, CODER_OK, TESTER_PASS], total=900, cost=300)

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    # The analyst burned its 300 in the analyze phase, so its adjudication is
    # not run -- but the coder and tester still get theirs, which is the whole
    # point of charging per seat.
    assert result["seat_spend"] == {"analyst": 300, "coder": 300, "tester": 300}
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst",
        "coder:r1",
        "tester:r1",
    ]
    assert any("analyst's seat is spent" in line for line in ctx.logs)
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_a_spent_coder_seat_ends_the_run_instead_of_calling_an_agent_that_cannot_run():
    # Round one leaves the coder with nothing while the analyst still has
    # enough to ask for a revision. Calling a coder that cannot take a step
    # would spend the round on an agent that returns nothing, so the run ends
    # and says why.
    revise = {
        "decision": "REVISE",
        "note": "the clamp is on the wrong bound",
        "implementation_task": "clamp the lower bound instead",
        "verification_task": "",
    }
    ctx = BudgetedCtx(
        [BRIEF, CODER_OK, TESTER_FAIL, revise, CODER_OK],
        total=1200,
        costs=[100, 400, 100, 100],
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["status"] == "budget_exhausted"
    assert result["seat_spend"]["coder"] == 400
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst",
        "coder:r1",
        "tester:r1",
        "analyst:adjudicate:r1",
    ]
    assert any("coder's seat is spent" in line for line in ctx.logs)


ANALYST_DIFF = (
    "diff --git a/pager.py b/pager.py\n"
    "--- a/pager.py\n+++ b/pager.py\n"
    "@@ -40,1 +40,1 @@\n-    end = start + size\n+    end = min(start + size, total)\n"
)
CODER_DIFF = ANALYST_DIFF + (
    "diff --git a/render.py b/render.py\n"
    "--- a/render.py\n+++ b/render.py\n"
    "@@ -7,1 +7,1 @@\n-    rows = page[:-1]\n+    rows = page\n"
)


@pytest.mark.asyncio
async def test_the_tree_is_recorded_at_each_phase_boundary_so_the_patch_can_be_attributed():
    # "the analyst wrote something" does not say how much of the delivered
    # patch it wrote, and after the coder runs nothing can recover it.
    ctx = ScriptedCtx(
        [BRIEF, CODER_OK, TESTER_PASS, ACCEPT],
        diffs=[ANALYST_DIFF, CODER_DIFF],
    )

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    snapshots = result["tree_snapshots"]
    assert [s["after"] for s in snapshots] == ["analyze", "implement:r1"]
    assert snapshots[0]["files"] == ["pager.py"]
    assert snapshots[1]["files"] == ["pager.py", "render.py"]
    assert snapshots[0]["chars"] == len(ANALYST_DIFF)
    assert snapshots[0]["sha256"] != snapshots[1]["sha256"]
    assert snapshots[0]["diff"] == ANALYST_DIFF


@pytest.mark.asyncio
async def test_a_tree_probe_that_cannot_answer_is_recorded_as_such_not_as_an_empty_diff():
    ctx = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT], diff=None)

    result = await self_collaboration(ctx, {"goal": "fix the pager"})

    assert result["tree_snapshots"][0] == {"after": "analyze", "diff": None}


@pytest.mark.asyncio
async def test_every_tree_probe_looks_past_the_tests_the_harness_injected():
    # The injected tests dirty the tree for the whole run and are not the
    # agents' edit; a probe that counts them reports a write that never
    # happened.
    ctx = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])

    await self_collaboration(
        ctx,
        {"goal": "fix the pager", "injected_test_paths": ["tests/test_pager.py"]},
    )

    assert ctx.source_changed_excludes == [
        ["tests/test_pager.py"],
        ["tests/test_pager.py"],
    ]


@pytest.mark.asyncio
async def test_the_reading_analyst_variant_takes_away_exactly_the_two_writing_tools():
    # The two variants have to differ by one thing or the difference between
    # them is not readable as one thing.
    writing = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])
    reading = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])

    await self_collaboration(writing, {"goal": "fix the pager"})
    await self_collaboration_reading_analyst(reading, {"goal": "fix the pager"})

    def names(ctx, index):
        return {getattr(t, "name", t) for t in (ctx.agent_calls[index]["tools"] or [])}

    assert names(writing, 0) - names(reading, 0) == {"apply_patch", "file_write"}
    # ... and only in the analyze phase: the analyst adjudicates with the full
    # bundle in both, so the arm's capability over the run is unchanged.
    assert names(writing, 3) == names(reading, 3)
    assert "apply_patch" in names(reading, 3)


@pytest.mark.asyncio
async def test_each_variant_records_which_one_it_was():
    writing = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])
    reading = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])

    a = await self_collaboration(writing, {"goal": "fix the pager"})
    b = await self_collaboration_reading_analyst(reading, {"goal": "fix the pager"})

    assert a["analyst_may_write_while_analysing"] is True
    assert b["analyst_may_write_while_analysing"] is False


@pytest.mark.asyncio
async def test_the_variant_changes_nothing_else_about_the_run():
    writing = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])
    reading = ScriptedCtx([BRIEF, CODER_OK, TESTER_PASS, ACCEPT])

    a = await self_collaboration(writing, {"goal": "fix the pager"})
    b = await self_collaboration_reading_analyst(reading, {"goal": "fix the pager"})

    assert writing.phases == reading.phases
    assert [c["label"] for c in writing.agent_calls] == [
        c["label"] for c in reading.agent_calls
    ]
    assert [c["prompt"] for c in writing.agent_calls] == [
        c["prompt"] for c in reading.agent_calls
    ]
    assert a["edges_walked"] == b["edges_walked"]
