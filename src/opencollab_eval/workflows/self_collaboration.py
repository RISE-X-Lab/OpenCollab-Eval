"""self-collaboration — the handoff team's topology, sequenced by code.

The code-sequenced twin of ``OpenCollab/configs/team.handoff.experiment.yaml``.
That team declares three roles and six directed edges and leaves the order of
work to the model; this workflow runs the same three roles over the same six
edges and lets no model decide the order. The one thing that differs between
the two arms is who sequences the exchange, which is the thing under study.

Why the topology has to match, edge for edge. The team's own header says the
return edges are the point: ``analyst -> coder`` and ``analyst -> tester`` with
nothing coming back is a closed star, and a prebuilt team wired that way is
mute. A twin that only briefed and never reported back would differ from the
team in two ways at once -- who sequences the work, and which edges exist at
all -- and a difference measured between them could then be read off either
one. So every one of the six edges carries a real payload here:

    analyst -> coder    the implementation task
    analyst -> tester   the verification task
    coder   -> tester   what was changed, in the coder's words
    coder   -> analyst  the implementation report, including any deviation
    tester  -> coder    the findings, when the verdict is FAIL
    tester  -> analyst  the verdict

and the run records which of them actually carried something, in
``edges_walked``, so the quantity this arm reports is the same quantity the
team arm reports: edges declared versus edges walked. On the team side that
number has been 6 declared and 0 walked across 37 runs.

Six declared is not six per run. ``tester -> coder`` carries the findings that
justify another attempt, and a run the tester passes on the first try has none
to send: five walked is what a clean run looks like here, and the sixth appears
only when there is a rejection to carry. Writing a payload onto that edge so
that every run reports six would be reporting a traversal the run did not need,
which is the failure this whole comparison exists to catch.

The analyst adjudicates at the end of every round, and that is not decoration:
it is what makes the two return edges real. The analyst is agent 0 on the team
-- the request arrives there and the answer read out is the one it gives -- so
a twin in which the analyst briefs once and never hears back has dropped the
role's defining property.

**Tools.** Each role holds the team's bundle minus the coordination tools.
``message_agent`` and ``team_status`` are scheduler-owned and have no referent
in a workflow: here the script is the only channel between two agents, and
that absence is this arm's definition rather than a gap in the mirror. What is
left is, for the analyst and the coder, exactly the single agent's working set
(``WORKING_TOOL_NAMES`` in ``gen_prediction_constants``), and for the tester
the team's tester bundle -- no ``apply_patch`` or ``file_write``, ``git_diff``
in their place. The analyst keeps the working tools even though the script
tells it to analyse: on the team it holds them so that the arm cannot be weaker
than the single agent it is compared against, and that reason survives being
sequenced.

**The analyst holding the working tools has a cost, and the run measures it.**
Keeping the single agent's bundle means the analyst can finish the task in the
analyze phase, before the script has called anyone -- and the first real run of
this workflow did exactly that: it spent 597,429 of a 600,000-token pool on
analysis, wrote the fix itself, left the coder 44,611 tokens, and the coder
returned nothing. The patch the harness graded was the analyst's. Nothing
downstream can detect this, because once the coder has run a changed tree is a
changed tree whoever changed it, so the tree is probed once between the two
phases and the answer is reported as ``analyst_wrote_source``. It is the same
question the team arm asks as adherence, asked of an arm where the sequence is
not the model's to choose.

**Budget is charged per seat, as on the team.** A prebuilt team gives each of
its N seats ``c * pool / N`` with ``c = 1.0``, so one agent cannot spend
another's share; a workflow draws from one pool and has no such rule, which is
how the analyst above was able to arrive at the coder's turn with the pool
already empty. Each call here is charged to its role and capped at
``pool / 3``, so the seat arithmetic is the team's and "the analyst ran out"
becomes a recorded fact (``seat_spend``, and a ``budget_exhausted`` status)
instead of a coder that silently never ran.

**One difference that is not held equal, and why.** On the team each agent gets
a git worktree of its own and a commit sha is the whole payload of a handoff.
This workflow runs all three in the shared tree at /testbed and asks nobody to
commit, which is what every workflow in this package already does and what the
harness grades: the answer is ``git diff`` of /testbed. Worktree isolation is
not available to a workflow inside the task container today --
``_workflow_runtime_session.acquire_isolated_env`` builds its ``WorktreePool``
without passing the session's container environment, so ``isolation=True``
would carve the worktree out of the host file system rather than the container
the repository actually lives in. Until that is closed, ``isolation=True`` here
would not mean what it says. The cost of the shared tree is stated rather than
hidden: a tester that shares the coder's directory holds the coder's edits
whether or not it was told about them, so this arm cannot answer "did the
tester work from what the coder handed it" the way a sha-based handoff can. It
answers the topology question, which is what it is for.

Select with ``--workflow self-collaboration`` in
``python -m opencollab_eval.generation.gen_prediction_workflow``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from opencollab.workflows import workflow

from ._public_api import toolset

# One repair round: at most two coder attempts. The team puts no bound on this
# because nothing there schedules a retry; the script must pick a number, and a
# larger one would make the arms differ in how many attempts the design buys
# rather than in who sequences them. The analyst can end the run earlier.
MAX_REPAIR_ROUNDS = 1

# The six declared edges, named once so that what is recorded as walked and
# what the team config declares are the same list.
DECLARED_EDGES = (
    "analyst->coder",
    "analyst->tester",
    "coder->tester",
    "coder->analyst",
    "tester->coder",
    "tester->analyst",
)

SHARED_RULES = """\
Rules:
- Prefer your dedicated tool over bash: file_read/grep to inspect, run_tests to
  test, file_write/apply_patch to edit. Use bash only for what no dedicated tool
  covers (for example a one-line `python -c` repro).
- Fix the root cause in the source; make the smallest correct change.
- Never edit test files.
- All three of you work in the same tree at /testbed. Leave your edits in the
  working tree: do not run `git commit`, and do not stash or revert another
  role's work.
- Keep your report tight: at most eight lines. What changed, why, and what the
  evidence for it is. No preamble.
"""

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "root_cause",
        "files",
        "implementation_task",
        "verification_task",
    ],
    "properties": {
        "root_cause": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        # The two outgoing edges of the analyst. They are separate fields
        # because they go to different agents: handing the tester the coder's
        # instructions would collapse two edges into one.
        "implementation_task": {"type": "string"},
        "verification_task": {"type": "string"},
    },
}

CODER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary_for_tester", "report_for_analyst"],
    "properties": {
        "summary_for_tester": {"type": "string"},
        "report_for_analyst": {
            "type": "string",
            "description": "What you did, and anything you had to decide that "
            "the brief did not settle -- a file the brief did not name, a "
            "different root cause than the one you were given, work you could "
            "not do. The analyst reads this and nothing else of yours.",
        },
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings_for_coder", "report_for_analyst"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings_for_coder": {
            "type": "string",
            "description": "On FAIL: the exact failing command, the error or "
            "traceback, and the suspected file and line. Empty on PASS.",
        },
        "report_for_analyst": {
            "type": "string",
            "description": "What you checked and what you concluded. On "
            "BLOCKED name the environmental blocker -- a missing dependency, "
            "no network, broken infrastructure -- not a code defect.",
        },
    },
}

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "note"],
    "properties": {
        "decision": {"type": "string", "enum": ["ACCEPT", "REVISE", "STOP"]},
        "note": {"type": "string"},
        # Present only on REVISE. These are the analyst's two outgoing edges
        # again, which is why a repair round is not just the coder retrying.
        "implementation_task": {"type": "string"},
        "verification_task": {"type": "string"},
    },
}

ANALYST_PROMPT = """\
You are the Analyst on a three-agent team: an Analyst, a Coder, and a Tester.
You are agent 0. The request arrives here and the answer read out at the end is
the one you give.

Read the task and the repository and work out what is actually broken. Then
write two separate instructions: one for the Coder, who will make the change,
and one for the Tester, who will check it. They are different people with
different jobs -- do not write the same paragraph twice.

Neither of them has read what you read. Whatever you want one of them to act on
travels only in the text you write here.

You hold the tools to make the change yourself. Whether you use them is your
own call and does not change what is asked of you at this step.

{rules}

Task:
{goal}
"""

CODER_PROMPT = """\
You are the Coder on a three-agent team: an Analyst, a Coder, and a Tester.
Make the change in /testbed and then report.

Your report has two readers and they need different things. The Tester needs to
know what to look at. The Analyst needs to know what you decided that the brief
did not settle.

{rules}

Task:
{goal}

The Analyst's brief:
{brief}

The Analyst's instruction to you:
{implementation_task}
{findings_block}
"""

FINDINGS_BLOCK = """
The Tester rejected the previous attempt. Do not repeat it. Its findings:
{findings}
"""

TESTER_PROMPT = """\
You are the Tester on a three-agent team: an Analyst, a Coder, and a Tester.
Verify the Coder's change adversarially.

Read the actual source in /testbed -- do not take the Coder's word for what is
there -- and run the checks that matter. Hunt for the failure: edge cases,
missing handling, a regression in neighbouring behaviour. You do not edit
files.

Verdict PASS only when the change is really in the tree and really satisfies
the Analyst's verification task. FAIL for a code defect. BLOCKED only when the
failure is environmental and no amount of further coding clears it.

Your report has two readers. The Coder needs findings concrete enough to act
on. The Analyst needs your verdict and what it rests on.

{rules}

Task:
{goal}

The Analyst's instruction to you:
{verification_task}

The Coder says it changed:
{coder_summary}
"""

ADJUDICATE_PROMPT = """\
You are the Analyst. Both of them have reported back. Decide what happens next.

{rules}

Task:
{goal}

Your brief was:
{brief}

The Coder reports:
{coder_report}

The Tester reports a verdict of {verdict}:
{tester_report}

Working tree since the Coder ran: {tree_state}

Choose one:

- ACCEPT — the change in /testbed answers the task. The run ends here and the
  tree is read as the answer.
- REVISE — another round is worth its cost. Write a new instruction for the
  Coder and a new one for the Tester; they replace the previous ones. Say what
  is different about this round, not what was already said.
- STOP — another round is not worth its cost, or the blocker is environmental.
  The run ends and the tree is read as it stands.

You may inspect the repository before deciding. You have {rounds_left} further
round(s) available; ACCEPT and STOP both end the run now.
"""


# A snapshot keeps enough of the tree's diff to attribute the delivered patch,
# and no more: the full text of a large diff would land in every metrics row.
DIFF_SNAPSHOT_CHARS = 20_000


def _diff_files(text: str) -> list[str]:
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2 and parts[1] not in paths:
                paths.append(parts[1])
    return paths


async def _snapshot(ctx: Any, after: str, into: list[dict[str, Any]]) -> None:
    """Record what the tree holds at a phase boundary.

    ``analyst_wrote_source`` says whether the analyst wrote anything; it cannot
    say how much of the delivered patch was already there before the coder was
    called, and once the coder has run nothing can reconstruct it. Taking the
    diff at each boundary makes that attribution a matter of comparing two
    recorded texts rather than of asking an agent what it did.
    """
    text = await ctx.diff()
    if text is None:
        into.append({"after": after, "diff": None})
        return
    into.append(
        {
            "after": after,
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "files": _diff_files(text),
            "diff": text[:DIFF_SNAPSHOT_CHARS],
            "truncated": len(text) > DIFF_SNAPSHOT_CHARS,
        }
    )


SEATS = 3


class _SeatBudget:
    """The team arm's budget rule, applied to a scripted run.

    A prebuilt team gives each of its N seats ``c * pool / N`` tokens with
    ``c = 1.0``, so one agent cannot spend another's share. Nothing enforces
    that here: a workflow draws from one pool, and the analyst -- which holds
    the working bundle and therefore can spend without limit -- would otherwise
    be able to drain it before the script has called anyone else. Charging each
    call to its seat keeps the two arms comparable on the axis that is supposed
    to be held equal, and makes "the analyst ran out" a recorded fact instead of
    a coder that silently never ran.

    Inert when the context reports no total, which is how the scripted
    stand-ins in the tests run.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.spent: dict[str, int] = {}
        total = getattr(getattr(ctx, "budget", None), "total", None)
        self.cap = (
            total // SEATS
            if isinstance(total, int) and not isinstance(total, bool) and total > 0
            else None
        )

    def remaining(self, seat: str) -> int | None:
        if self.cap is None:
            return None
        return self.cap - self.spent.get(seat, 0)

    async def run(self, seat: str, call: Any) -> Any:
        left = self.remaining(seat)
        before = self._ctx.tokens_spent()
        result = await call(left)
        self.spent[seat] = self.spent.get(seat, 0) + max(
            0, self._ctx.tokens_spent() - before
        )
        return result


def _analyst_tools() -> list[Any]:
    # The single agent's working set, which is also the team analyst's bundle
    # minus message_agent and team_status.
    return toolset(
        "apply_patch", "bash", "file_read", "file_write", "grep", "run_tests", "submit"
    )


def _reading_analyst_tools() -> list[Any]:
    """The analyst's bundle with the two tools that write a file removed.

    Used only by the ``-reading-analyst`` variant, and only for the analyze
    phase: the analyst still holds the full bundle when it adjudicates, so the
    arm's capability over the whole run is unchanged and the difference between
    the two variants is exactly "could the analyst do the work before the coder
    was called". Starving the analyst for a whole run is a different and
    forbidden change -- it would make the arm weaker than the single agent it
    is compared against, readable straight off the tool list.
    """
    return toolset("bash", "file_read", "grep", "run_tests", "submit")


def _coder_tools() -> list[Any]:
    return toolset(
        "apply_patch", "bash", "file_read", "file_write", "grep", "run_tests", "submit"
    )


def _tester_tools() -> list[Any]:
    # No apply_patch and no file_write: the team's tester holds git_diff in
    # their place, which is a declared role boundary rather than an arm
    # difference, so it carries over unchanged.
    return toolset("bash", "file_read", "git_diff", "grep", "run_tests", "submit")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _text(payload: Any, key: str) -> str:
    return str(payload.get(key) or "").strip() if isinstance(payload, dict) else ""


def _tree_state(source_changed: bool | None) -> str:
    if source_changed is True:
        return "the source has been changed"
    if source_changed is False:
        return "the source is unchanged -- nothing has been written"
    return "unknown"


async def _run(
    ctx: Any, args: dict[str, Any], *, analyst_may_write: bool
) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    # An edge counts as walked when a payload actually crossed it, not when the
    # script reached the line that would have sent one: an analyst that returns
    # an empty verification_task has left the analyst -> tester edge unwalked,
    # and that is exactly the kind of silence this arm exists to measure.
    # The harness dirties the tree with the tests it injects, which are not the
    # agents' edit: every tree probe here has to look past them or a run that
    # wrote nothing reads as a run that wrote something.
    injected = [str(p) for p in (args.get("injected_test_paths") or [])]
    walked: list[str] = []
    snapshots: list[dict[str, Any]] = []
    seats = _SeatBudget(ctx)

    def exhausted(seat: str) -> bool:
        left = seats.remaining(seat)
        return left is not None and left <= 0

    def walk(edge: str, payload: str) -> str:
        if payload and edge not in walked:
            walked.append(edge)
        return payload

    await ctx.phase("analyze")
    brief = await seats.run(
        "analyst",
        lambda budget: ctx.agent(
            ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
            schema=BRIEF_SCHEMA,
            label="analyst",
            tools=_analyst_tools() if analyst_may_write else _reading_analyst_tools(),
            budget=budget,
        ),
    )
    # The analyst holds the working bundle -- the same seven tools the single
    # agent holds -- so nothing stops it from fixing the task here, before the
    # script has called anyone. Whether it did is the question the team arm asks
    # as alpha, and after the coder has run no later probe can answer it: a
    # changed tree is a changed tree whoever changed it. So ask now, while the
    # answer still names one agent.
    analyst_wrote_source = await ctx.source_changed(injected)
    await _snapshot(ctx, "analyze", snapshots)

    if not isinstance(brief, dict):
        return {
            "status": "error",
            "error": "analyst produced no structured brief",
            "analyst_wrote_source": analyst_wrote_source,
            "tree_snapshots": snapshots,
            "edges_declared": list(DECLARED_EDGES),
            "edges_walked": walked,
            "tokens_spent": ctx.tokens_spent(),
        }

    implementation_task = walk("analyst->coder", _text(brief, "implementation_task"))
    verification_task = walk("analyst->tester", _text(brief, "verification_task"))
    findings = ""
    rounds: list[dict[str, Any]] = []
    status = "incomplete"

    for round_no in range(1, MAX_REPAIR_ROUNDS + 2):
        await ctx.phase("implement")
        if exhausted("coder"):
            await ctx.log(f"round {round_no}: the coder's seat is spent")
            status = "budget_exhausted"
            break
        patch = await seats.run(
            "coder",
            lambda budget: ctx.agent(
                CODER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    brief=_dump(brief),
                    implementation_task=implementation_task
                    or "(the analyst left this empty -- work from the brief)",
                    findings_block=(
                        FINDINGS_BLOCK.format(findings=findings) if findings else ""
                    ),
                ),
                schema=CODER_SCHEMA,
                label=f"coder:r{round_no}",
                tools=_coder_tools(),
                budget=budget,
            ),
        )
        coder_summary = walk("coder->tester", _text(patch, "summary_for_tester"))
        coder_report = walk("coder->analyst", _text(patch, "report_for_analyst"))
        source_changed = await ctx.source_changed(injected)
        await _snapshot(ctx, f"implement:r{round_no}", snapshots)

        await ctx.phase("verify")
        if exhausted("tester"):
            await ctx.log(f"round {round_no}: the tester's seat is spent")
            status = "budget_exhausted"
            break
        verdict_payload = await seats.run(
            "tester",
            lambda budget: ctx.agent(
                TESTER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    verification_task=verification_task
                    or "(the analyst left this empty -- verify against the task)",
                    coder_summary=coder_summary or "(the coder reported nothing)",
                ),
                schema=VERDICT_SCHEMA,
                label=f"tester:r{round_no}",
                tools=_tester_tools(),
                budget=budget,
            ),
        )
        verdict = str(_text(verdict_payload, "verdict") or "FAIL").upper()
        tester_findings = walk(
            "tester->coder", _text(verdict_payload, "findings_for_coder")
        )
        tester_report = walk(
            "tester->analyst", _text(verdict_payload, "report_for_analyst")
        )

        # A PASS on a tree nobody wrote to is a report about the tester, not
        # about the change; the harness reads /testbed either way, so let the
        # analyst see the contradiction rather than resolving it here.
        rounds_left = MAX_REPAIR_ROUNDS + 1 - round_no

        await ctx.phase("adjudicate")
        if exhausted("analyst"):
            # The analyst spent its seat on the analyze phase. Its verdict is
            # not available, so the tester's stands -- the same fallback a dead
            # analyst gets below, reached for a reason worth naming separately.
            await ctx.log(
                f"round {round_no}: the analyst's seat is spent — "
                f"deferring to the tester's {verdict}"
            )
            decision_payload = None
        else:
            decision_payload = await seats.run(
                "analyst",
                lambda budget: ctx.agent(
                    ADJUDICATE_PROMPT.format(
                        rules=SHARED_RULES,
                        goal=goal,
                        brief=_dump(brief),
                        coder_report=coder_report or "(the coder reported nothing)",
                        verdict=verdict,
                        tester_report=tester_report or "(the tester reported nothing)",
                        tree_state=_tree_state(source_changed),
                        rounds_left=rounds_left,
                    ),
                    schema=DECISION_SCHEMA,
                    label=f"analyst:adjudicate:r{round_no}",
                    tools=_analyst_tools(),
                    budget=budget,
                ),
            )
        decision = str(_text(decision_payload, "decision") or "").upper()
        if decision not in {"ACCEPT", "REVISE", "STOP"}:
            # No usable decision is not the same as a decision to continue: fall
            # back to the tester's verdict so a dead analyst cannot silently buy
            # the run another round.
            decision = "ACCEPT" if verdict == "PASS" else "STOP"
            await ctx.log(
                f"round {round_no}: analyst returned no usable decision — "
                f"falling back to {decision} on the tester's {verdict}"
            )

        rounds.append(
            {
                "round": round_no,
                "coder_summary": coder_summary,
                "coder_report": coder_report,
                "source_changed": source_changed,
                "verdict": verdict,
                "tester_findings": tester_findings,
                "tester_report": tester_report,
                "decision": decision,
                "note": _text(decision_payload, "note"),
            }
        )

        if decision == "ACCEPT":
            status = "done"
            break
        if decision == "STOP":
            status = "blocked" if verdict == "BLOCKED" else "stopped"
            break
        if rounds_left <= 0:
            await ctx.log(
                f"round {round_no}: analyst asked to revise with no rounds left"
            )
            break

        # REVISE re-walks both of the analyst's outgoing edges. A revision that
        # names no new instruction leaves the previous one standing rather than
        # handing the coder an empty task.
        implementation_task = (
            walk("analyst->coder", _text(decision_payload, "implementation_task"))
            or implementation_task
        )
        verification_task = (
            walk("analyst->tester", _text(decision_payload, "verification_task"))
            or verification_task
        )
        findings = tester_findings or "The tester rejected the change without findings."

    return {
        "status": status,
        "root_cause": _text(brief, "root_cause"),
        "files": brief.get("files", []),
        "analyst_may_write_while_analysing": analyst_may_write,
        "analyst_wrote_source": analyst_wrote_source,
        "tree_snapshots": snapshots,
        "seat_cap": seats.cap,
        "seat_spend": dict(seats.spent),
        "rounds": rounds,
        "edges_declared": list(DECLARED_EDGES),
        "edges_walked": walked,
        "tokens_spent": ctx.tokens_spent(),
    }


@workflow(
    name="self-collaboration",
    description="Analyst, coder, and tester over the handoff team's six edges, "
    "sequenced by code instead of by the model.",
    phases=["analyze", "implement", "verify", "adjudicate"],
)
async def self_collaboration(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return await _run(ctx, args, analyst_may_write=True)


@workflow(
    name="self-collaboration-reading-analyst",
    description="self-collaboration with an analyst that can only read while it "
    "analyses, so the work cannot be done before the coder is called.",
    phases=["analyze", "implement", "verify", "adjudicate"],
)
async def self_collaboration_reading_analyst(
    ctx: Any, args: dict[str, Any]
) -> dict[str, Any]:
    """The same run with one thing taken away, so the difference names itself.

    Its sibling's analyst finishes the task during the analyze phase and the
    coder then edits a tree that already answers the request. Here the analyst
    cannot write until it adjudicates, so whatever reaches the delivered patch
    passed through the coder. Everything else -- roles, edges, prompts, seat
    budgets, the tester's bundle -- is the same object.
    """
    return await _run(ctx, args, analyst_may_write=False)


__all__ = ["self_collaboration", "self_collaboration_reading_analyst"]
