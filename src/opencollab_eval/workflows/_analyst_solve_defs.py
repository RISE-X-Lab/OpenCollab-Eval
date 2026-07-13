"""analyst-solve — analyst-driven reconnaissance, then a phased coder/tester build.

Sibling of ``scout_solve.py`` and ``self_collab.py``. It grafts ``scout_solve``'s
parallel read-only reconnaissance onto ``self_collab``'s phased coder/tester loop,
but the ANALYST stays in charge end to end: it first decomposes the problem into
exploration dimensions, then — after the scouts report — designs the phased fix
itself instead of handing off to a separate synthesizer.

Built for hard tasks where a single shallow pass already failed. Three levers
distinguish it from the siblings:

* it pays for breadth of reconnaissance up front (parallel scouts), so the plan
  starts from a confirmed root cause rather than a guess;
* phases run BEST-EFFORT — a failed phase does not stop the run (it leaves its
  partial edits and the next phase continues), because a partial patch grades
  better than none;
* a budget floor guarantees output: before every expensive step it reserves
  headroom, and if the budget runs low it bails to a single ``forced-write``
  coder whose only job is to land a concrete edit, right or wrong.

Shape:

* analyst (scope) decomposes the PROBLEM into independent exploration dimensions;
* each dimension is investigated in parallel by a read-only scout;
* analyst (plan) synthesizes the findings into a root cause, an approach, and an
  ordered list of implementation phases;
* each phase runs a sequential coder -> tester loop, best-effort;
* a final whole-goal verification gets one repair round if the budget allows.

Select with ``--workflow analyst-solve`` in
``python -m opencollab_eval.generation.gen_prediction_workflow``.

The eval harness runs it unchanged: ``goal`` falls back to the task
``description`` that ``run_eval_task`` passes in its args dict.
"""

from __future__ import annotations

from typing import Any

from opencollab.sdk import (
    ApplyPatchTool,
    BashTool,
    FileReadTool,
    FileWriteTool,
    GrepTool,
    verification_run_tests_tool,
)
from opencollab.sdk.experimental import (
    ENFORCEMENT_OFF,
)

# Rounds a single phase gets before the run moves on (best-effort, no stop).
MAX_ROUNDS_PER_PHASE = 4
# Token headroom kept in reserve. Once the remaining budget drops below this, the
# run abandons further loops and spends the reserve on one forced-write coder so
# the working tree is never left empty. Sized for one forced write PLUS a final
# verify (FORCED_WRITE_BUDGET + TESTER_BUDGET), so verify is never starved.
RESERVE_TOKENS = 350_000

# Per-call token caps passed as ``budget=`` to each ctx.agent. Anchored on the
# real per-role spend measured from instrumented runs (healthy scouts 140-230k;
# scope/plan analysts ~90-130k; the implement coder needs the bulk). Each cap
# bounds a SINGLE runaway session — e.g. a non-converging scout that snowballs
# its context past 700k and drains the whole pool — without throttling a
# legitimately hard step; the framework clamps every cap to the live global
# remaining, so the shared pool is never overshot. Allocation is per CALL, not
# per role: analyst:scope and analyst:plan are the same role yet get separate
# caps, which is what lets us throttle the scope call (it snowballed to 400k)
# without touching plan.
SCOPE_BUDGET = 200_000
SCOUT_BUDGET = 250_000
PLAN_BUDGET = 150_000
CODER_BUDGET = 350_000
TESTER_BUDGET = 200_000
FORCED_WRITE_BUDGET = 120_000
REPAIR_BUDGET = 200_000
# Cap on parallel recon scouts so recon's total is bounded no matter how many
# dimensions the scope analyst invents.
MAX_SCOUTS = 4

# Budget the recon phase MUST leave untouched for the rest of the run (plan +
# implement/forced-write + verify). Recon scouts are read-only, so the
# reads_since_last_edit write-nudge never brakes them; each fills its cap
# exploring. With a FIXED per-scout cap their sum (SCOUT_BUDGET * MAX_SCOUTS ~=
# 1M) drains a 1M pool inside recon before implement/verify ever run — measured:
# recon ate 66-92% of a 1M budget, 7/8 instances ended with empty completions.
# Fix: derive the scout cap from the LIVE remaining minus this floor, so the
# scouts collectively can never dip below it:
#     scout_cap = min(SCOUT_BUDGET, (remaining - RECON_FLOOR) // n_scouts)
# At 2M the SCOUT_BUDGET ceiling binds and the tail is naturally safe; at 1M the
# floor binds and throttles scouts so plan/implement/verify always keep this
# reserve (which exceeds RESERVE_TOKENS, leaving room for plan + a forced write +
# a final verify). This is the "deduct recon, guarantee the tail" rule — it makes
# the steering hint's per-call-cap blindness harmless because recon is bounded.
#
# Raised 400k -> 600k so the implement loop's round-1 gate can actually FIRE:
# after recon leaves this floor and plan spends PLAN_BUDGET (150k), the tail keeps
# 600k - 150k = 450k > RESERVE_TOKENS (350k), so implement runs a real
# coder/tester round instead of ALWAYS bailing to a forced write (at 400k the tail
# was 400k-150k=250k < 350k -> gate never fired). This starves the measured
# ~91%-over-funded scouts from 150k -> 100k each (recon_pool 600k -> 400k at 1M),
# which they do not need (they re-read one core file 12-26x well under cap).
RECON_FLOOR = 600_000

# Shared rules — every role gets them (lifted from configs/team.self.collab.yaml,
# the SWE-bench-tuned variant: it warns off chasing not-yet-existing tests).
SHARED_RULES = """\
Rules:
- Prefer your DEDICATED tool over bash: file_read/grep to inspect, run_tests \
to test, file_write/apply_patch to edit. Use bash ONLY for what no dedicated \
tool covers (e.g. a one-line `python -c` repro).
- Fix the ROOT CAUSE in the source; make the SMALLEST correct change.
- NEVER edit test files. NEVER run `git commit`; leave edits in the working tree.
- Never assume a package is available: confirm the repo already imports it \
(grep / check the manifest) before using it, and verify your own imports \
resolve before reporting done.
- Keep reports free of preamble and postamble. A STATUS report (what changed, \
why, the verification result) stays under ~8 lines. But when your job is to \
surface EVIDENCE — a scout answering its dimension, or a coder citing exactly \
what it changed — give the next agent the full detail it needs: exact file \
paths, line numbers, and the quotes that matter. Never drop evidence to fit a \
line count.
- Do NOT grep for a FAIL_TO_PASS test that does not exist yet — the task may \
require creating it; chasing a missing test wastes budget."""

DIMENSIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["dimensions"],
    "properties": {
        "initial_read": {
            "type": "string",
            "description": "One or two sentences on your first read of the problem — optional context for the scouts.",
        },
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["aspect", "question", "hints"],
                "properties": {
                    "aspect": {"type": "string", "description": "Short name for this angle, e.g. 'bug origin'."},
                    "question": {
                        "type": "string",
                        "description": "The concrete, independently-answerable question this scout must resolve.",
                    },
                    "hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Where to start looking — files, dirs, symbols (may be empty).",
                    },
                },
            },
        },
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause", "approach", "phases"],
    "properties": {
        "root_cause": {"type": "string", "description": "The confirmed root cause the reconnaissance supports."},
        "approach": {"type": "string", "description": "The smallest correct fix the evidence supports."},
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["goal", "files", "done"],
                "properties": {
                    "goal": {"type": "string", "description": "ONE unit of work."},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "done": {"type": "string", "description": "A concrete, testable definition of done."},
                },
            },
        },
    },
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "tests_run", "failed_count"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {
            "type": "string",
            "description": "On FAIL: the exact failing command, error/traceback, suspected file/line. "
            "On BLOCKED: name the environmental blocker (missing dependency, no network, "
            "broken/unrelated infra) — not a code defect — so it can be surfaced upward.",
        },
        "tests_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The exact test node-ids you actually executed with run_tests this "
            "verification — proof, not a label. For a graded task this MUST include every "
            "target (FAIL_TO_PASS) node-id you were given.",
        },
        "failed_count": {
            "type": "integer",
            "minimum": 0,
            "description": "How many of the tests you ran failed or errored (0 for a clean PASS). "
            "Read it straight off run_tests' Counts line; do not estimate.",
        },
    },
}

SCOPE_PROMPT = """\
You are the Analyst. Do NOT solve and do NOT plan a fix yet — your job here is to \
frame the investigation. Read the goal and skim the codebase (file_read, grep; \
bash only for a one-line `python -c` behavior trace) just enough to decompose the \
PROBLEM into INDEPENDENT exploration dimensions: distinct angles that, \
investigated in parallel, surface everything needed to solve it correctly — e.g. \
where the defect originates, how the relevant subsystem actually works, what the \
tests/spec expect, what callers and contracts depend on it, and the edge cases. \
Each dimension is ONE focused, read-only question with a hint about where to \
start. Dimensions must be answerable independently and in any order — no scout \
should need another's result. Size by the actual problem: a couple of sharp \
dimensions beat many shallow ones; aim for two to four.

{rules}

Goal:
{goal}
{target_tests}"""

# Surfaces the FAIL_TO_PASS node-ids the run is graded on WITHOUT echoing the
# tests' literal assertion values — naming a test's expected output invites
# overfitting to that one input. We hand over the node-ids + an instruction to
# read the behavior and fix the ROOT CAUSE for the whole class of inputs.
TARGET_TESTS_BLOCK = """
Target tests (graded on these — fix the ROOT CAUSE, do not overfit):
{ids}
These node-ids name the BEHAVIOR your fix must satisfy. Read each test to \
understand the behavior it checks, but do NOT special-case the test's literal \
assertion values — fix the underlying defect for the whole class of inputs so \
the behavior is correct in general, not just for these exact cases."""

SCOUT_PROMPT = """\
You are a Scout investigating ONE dimension of a larger problem. You do NOT edit \
anything — this is read-only reconnaissance (file_read, grep; bash only for a \
one-line `python -c` trace). Answer your dimension's question thoroughly and \
concretely: cite exact files and line numbers, quote the code that matters, and \
spell out the contracts, edge cases, and risks you find. Do not propose a full \
fix — surface the evidence the planner will need. Your final message IS your \
findings report: dense, specific, and backed by what you actually read.

{rules}

Overall goal (for context only — answer your dimension, not the whole goal):
{goal}

Your dimension — {aspect}:
{question}

Where to start:
{hints}{draft_block}"""

# STEP 5b commit-first: the bounded submit-only prompt that produces a scout's
# turn-0 DRAFT from the STATIC fact sheet alone (no reads). The draft anchors a
# committed cite-or-abstain artifact BEFORE exploration; the scout then revises it.
# Cite-or-abstain: fact-sheet-only, so every draft finding is verified=false until
# the scout's own read confirms it (NOT fabrication — an honest unconfirmed draft).
DRAFT_PROMPT = """\
You are a Scout about to investigate ONE dimension of a problem. BEFORE you read \
anything, commit a DRAFT of your findings based ONLY on the static fact sheet below — \
call submit_findings now. This draft anchors your investigation; you will revise it \
with real evidence in the next step.

Cite-or-abstain: the fact sheet is STATIC and NOT YET confirmed by your own reads, so \
mark EVERY draft finding verified=false and use lower confidence. You MAY set \
evidence_anchor to a location the fact sheet lists (e.g. a file:line) as a POINTER to \
check, but keep verified=false until one of your own reads confirms it. If the fact \
sheet is too thin to draft anything for this dimension, set insufficient_evidence=true \
— do NOT fabricate findings or anchors.

Your dimension — {aspect}:
{question}

Static fact sheet:
{fact_hint}

Where you will look next:
{hints}"""

# Appended to SCOUT_PROMPT when a draft was committed: frame the reads as REVISION
# of the committed draft (the scout's refined submit, not this draft, is harvested).
DRAFT_REVISE_BLOCK = """

Your committed draft (from the static fact sheet — a hypothesis, NOT a conclusion):
{draft}

Revise and STRENGTHEN this draft with real evidence: confirm each finding against the \
actual source with your reads and upgrade confirmed ones to verified=true with a real \
file:line / matched-string anchor, correct anything the fact sheet got wrong, and add \
what it missed. Then re-commit your refined findings with submit_findings — that \
refined submission, not the draft, is your report."""

PLAN_PROMPT = """\
You are the Analyst, now designing the solution. Reconnaissance is complete — the \
scouts' findings are below. Synthesize them into a concrete plan: the confirmed \
root cause, the approach (the smallest correct change the evidence supports), and \
an ordered list of implementation phases. Each phase is exactly ONE unit of work \
with a focused file set and a concrete, testable definition of done. Size phases \
by the actual work — most fixes are a SINGLE phase; split into multiple only when \
the work has genuinely independent parts better implemented and verified \
separately. Trust the findings but confirm anything decisive against the source \
yourself (file_read/grep) before committing it to the plan. Do NOT edit anything. \
Every target test's behavior below MUST be covered by some phase's definition of \
done — but define done by the corrected behavior, not by the test's literal values.

{rules}

Goal:
{goal}
{target_tests}

Reconnaissance findings:
{findings}"""

CODER_PROMPT = """\
You are a Coder implementing ONE phase of a planned fix. A reconnaissance pass \
already mapped this problem; work from the context and phase below, but verify \
anything decisive in the source before you rely on it. Inspect with \
file_read/grep. Default edit: file_write in str_replace mode — minimal and \
targeted. If str_replace fails twice (no unique match — whitespace diff, \
duplicate/ambiguous lines, line drift), do NOT retry the same replacement: fall \
back to apply_patch with a content-anchored diff (use line_replace with \
expected_str to guard the range). Verify with run_tests (or a short `python -c` \
repro) before reporting. Your final message is your report: what you changed \
(each file + edit), why, and your verification result.

{rules}

Overall goal:
{goal}

Confirmed root cause:
{root_cause}

Overall approach:
{approach}

This phase — {phase_goal}

Files to touch:
{files}

Definition of done:
{done}
{target_tests}
{findings_block}"""

FINDINGS_BLOCK = """
A previous attempt FAILED verification. Do not repeat it — address these \
concrete findings from the tester:
{findings}"""

TESTER_PROMPT = """\
You are a Tester adversarially verifying a coder's change. Run the project's \
tests with run_tests. Inspect the ACTUAL source with file_read/grep — do not \
trust the coder's summary; confirm the change is really there and really fixes \
the root cause. Hunt failures: edge cases, missing handling, regressions in \
neighboring behavior. You do not edit files.

Proof, not a label. If target tests are named below, you MUST run them with \
run_tests using those EXACT node-ids (pass them as the `target`) and report them \
in `tests_run`; report the failed/errored total in `failed_count` straight off \
run_tests' Counts line. NEVER self-certify with `python -c` or by eyeballing the \
source — only a real run_tests execution of the named node-ids counts. \
PASS requires that EVERY named target node-id appears in `tests_run`, is in the \
run's passed set, and `failed_count` is 0 (zero failed, zero errored).

Verdict PASS only when the change is really there, the named target tests pass \
with zero failures, and the definition of done holds. Verdict FAIL for a code \
defect (including any target test still failing). Verdict BLOCKED only when the \
failure is ENVIRONMENTAL — a missing dependency, no network, or broken/unrelated \
infra — not something more coding can fix; name the blocker in findings so it can \
be surfaced upward instead of burning more rounds.

{rules}

Goal:
{goal}
{target_tests}

Definition of done:
{done}

Coder's report:
{summary}"""

STATIC_TESTER_PROMPT = """\
You are a Tester verifying a coder's change in an environment with NO runnable test \
suite (no pytest, heavy deps like torch absent, grading tests withheld by design). Do \
NOT call run_tests or pytest. You are GIVEN the plan the coder worked to and the \
coder's report below — verify the edit against THOSE; do NOT go re-derive the spec by \
exploring the codebase. Do EXACTLY these two things, then STOP and emit your verdict:

A. STATIC CHECKS on the edited file(s):
   - grep the edited source ONCE for `raise NotImplementedError` and once for \
`# TODO: implement this function` — if the target body is still a stub, FAIL.
   - run `python3 -m py_compile <edited_file>` once per edited file — any SyntaxError \
is a FAIL (report file:line).
   - signature: confirm the function name and parameters match the plan/goal; a \
renamed/added/dropped param is a FAIL.
B. PLAN CONSISTENCY: read the edited function ONCE and check it does what the PLAN's \
approach and definition of done describe — every branch / behavior the plan names is \
present and not contradicted. A missing or contradicted plan item is a FAIL (name it).

That is ALL. Do NOT open unrelated files, do NOT re-grep a pattern you already ran, do \
NOT run toy tests, do NOT re-derive the spec from scratch. The moment A and B resolve, \
call structured_output: PASS when A is clean AND B is consistent; FAIL with SPECIFIC, \
ACTIONABLE findings (which file, what is wrong or missing) so the coder can fix it. \
Reserve BLOCKED for genuine infra breakage unrelated to the code — NEVER for \
"pytest/torch missing", which is expected here. Set `tests_run` to [] and \
`failed_count` to 0.

{rules}

Goal:
{goal}
{target_tests}

The plan the coder implemented (verify the edit against THIS — do not re-derive it):
- root cause: {root_cause}
- approach: {approach}

Definition of done:
{done}

Coder's report:
{summary}"""

FORCED_PROMPT = """\
You are a Coder and the token budget is nearly exhausted — this is the LAST \
action of the run. STOP investigating. Based on the confirmed root cause, the \
approach, and whatever edits are already in the working tree, implement the \
single most likely correct fix RIGHT NOW. You MUST leave concrete edits in the \
working tree (file_write or apply_patch) — a reasonable attempt is far better \
than no patch at all. Do not run the full test suite; at most a quick \
`python -c` sanity check. Then report in <=5 lines.

{rules}

Goal:
{goal}

Confirmed root cause:
{root_cause}

Approach:
{approach}

Work already attempted (for context):
{progress}"""

COMMIT_PROMPT = """\
You are a Coder. You have analyzed this phase but the working tree is still \
unchanged — no edit has landed. STOP investigating and implement the fix NOW: \
based on the confirmed root cause and approach, make the single most likely \
correct edit with file_write or apply_patch this turn. A concrete attempt is far \
better than more analysis; you can refine it after the tester runs. Then report \
in <=5 lines.

{rules}

Goal:
{goal}

Confirmed root cause:
{root_cause}

Approach:
{approach}

Progress so far:
{progress}"""

# Whole-goal definition of done for the final verification pass.
FINAL_DONE = (
    "The issue described in the goal is resolved at its root cause; the named "
    "FAIL_TO_PASS target tests run green with zero failures; and existing and "
    "neighboring tests still pass (no regressions)."
)


def _target_tests_block(args: dict[str, Any]) -> str:
    """Render the FAIL_TO_PASS node-ids as a behavior hint (or empty string).

    Anti-overfit by construction: we surface only the node-ids — never the
    tests' literal assertion values — plus a fix-the-root-cause instruction.
    Empty when no ids were threaded in (CLI runs, non-SWE-bench tasks), so the
    prompts collapse back to their original shape.
    """
    ids = args.get("fail_to_pass") or []
    if not ids:
        return ""
    listed = "\n".join(f"- {i}" for i in ids)
    return TARGET_TESTS_BLOCK.format(ids=listed)


def _verified_test_targets(tools: list[Any]) -> set[str]:
    """Collect parser-backed GREEN targets from this tester call's tool instance."""
    run_tests = next((tool for tool in tools if getattr(tool, "name", "") == "run_tests"), None)
    return set(getattr(run_tests, "verified_targets", ()))


def _f2p_gate(
    verdict: Any,
    fail_to_pass: list[str],
    *,
    executed_tests: set[str] | None = None,
) -> str | None:
    """Hard-gate a tester PASS on the real FAIL_TO_PASS node-ids (D2).

    Returns ``None`` when the PASS may stand, or a findings string when it must
    be overridden to not-passed. The gate fires whenever ``fail_to_pass`` is
    non-empty, regardless of whether a test patch was supplied. Defense in
    depth: even a PASS verdict must carry ``failed_count == 0``, every required
    node-id in ``tests_run``, and (when supplied by the workflow) parser-backed
    GREEN evidence from this tester call's run_tests instance.
    """
    if not fail_to_pass:
        return None  # no benchmark target ids were declared
    if not isinstance(verdict, dict):
        return None  # not a PASS to override; the caller handles dead/FAIL verdicts
    failed = verdict.get("failed_count")
    if type(failed) is not int or failed != 0:
        return (
            f"Tester reported {failed!r} failed/errored test(s), which is invalid. "
            "The named FAIL_TO_PASS "
            "tests must run green with ZERO failures. Re-run the exact target node-ids "
            "with run_tests and fix the remaining failures."
        )
    ran = verdict.get("tests_run")
    ran_set = set(ran) if isinstance(ran, list) else set()
    missing = [nid for nid in fail_to_pass if nid not in ran_set]
    if missing:
        listed = ", ".join(missing)
        return (
            "These required FAIL_TO_PASS node-ids were not shown as executed in the "
            f"verification: {listed}. Run them with run_tests using the EXACT node-ids "
            "and ensure they pass with zero failures before reporting PASS."
        )
    if executed_tests is not None:
        unproved = [nid for nid in fail_to_pass if nid not in executed_tests]
        if unproved:
            listed = ", ".join(unproved)
            return (
                "This tester call contains no parser-backed GREEN run_tests execution for these "
                f"required node-ids: {listed}. Run each exact target with run_tests; a "
                "tests_run self-report cannot replace executable evidence."
            )
    return None


def _read_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), GrepTool()]


# Enforced-mode prompt suffixes (Phase 1, 1D). Appended ONLY when enforcement is
# on — the base PLAN_PROMPT/CODER_PROMPT/FORCED_PROMPT/COMMIT_PROMPT strings are
# never edited, so the OFF prompt text stays byte-for-byte identical to reference.
PLANNER_ENFORCED_SUFFIX = (
    "\n\nYou have ONLY file_read + grep (no shell, no file editing). Confirm facts "
    "by reading/grepping; never run commands or write files."
)
CODER_ENFORCED_SUFFIX = (
    "\n\nYou have NO shell/bash and CANNOT create new files. Edit ONLY the existing "
    "target via file_write str_replace (preferred) or apply_patch. Do NOT attempt "
    "to run python/tests via shell or write helper/test scripts. "
    "Before you USE any config attribute, field name, method, or function signature "
    "you are not certain of, file_read/grep the defining source to confirm its EXACT "
    "name — never invent or guess an attribute/field name (a wrong attribute crashes "
    "at runtime). If the reconnaissance findings name it, copy that name verbatim."
)


def _enforcement_on(enforcement_strength: str) -> bool:
    """True when the structural toolset whitelist (Phase 1) is engaged.

    Mirrors ``SessionRun._enforcement_on`` so the workflow can decide locally
    whether to swap the toolset and append the enforced prompt suffix. ``off`` ->
    False -> reference behavior.
    """
    return enforcement_strength != ENFORCEMENT_OFF


def _planner_suffix(enforcement_strength: str) -> str:
    return PLANNER_ENFORCED_SUFFIX if _enforcement_on(enforcement_strength) else ""


def _coder_suffix(enforcement_strength: str) -> str:
    return CODER_ENFORCED_SUFFIX if _enforcement_on(enforcement_strength) else ""


# Enforced-mode RECON PASS-THROUGH (lever 1). The coder normally sees only the
# planner's distilled root_cause/approach — a LOSSY compression that drops the
# concrete facts the scouts paid real tokens to confirm (exact config field names,
# formulas, signatures). Observed failure: a scout found the canonical DAPO overlong
# formula + field `penalty_factor`, the plan compressed it to "follow the documented
# algorithm", and the coder — never given the finding — invented a nonexistent
# attribute and crashed. This carries the FULL findings document through to the coder
# so those facts survive. Appended (never edits the base CODER_PROMPT); OFF -> "".
RECON_FACTS_HEADER = (
    "\n\nReconnaissance findings — the scouts already read the actual source/docs and "
    "confirmed the facts below; this cost real exploration and is authoritative (the "
    "planner's summary above may have dropped specifics). Use the EXACT names, "
    "signatures, config field names, formulas, and values stated here VERBATIM — do NOT "
    "re-derive or invent them. Where a finding gives a concrete identifier or formula, "
    "copy it; do not substitute your own:\n"
)


def _recon_block(recon_findings: str, enforcement_strength: str) -> str:
    """Enforced-mode ONLY: full scout findings appended to a coder prompt so concrete
    recon facts survive the planner's compression. OFF -> "" (reference byte-identical).
    Returns "" for the recon-skipped placeholder (no real facts to carry)."""
    if not _enforcement_on(enforcement_strength):
        return ""
    body = (recon_findings or "").strip()
    if not body or body.startswith("(reconnaissance skipped"):
        return ""
    return RECON_FACTS_HEADER + body


def _final_verify_redundant(enforcement_strength: str, forced: bool, phase_reports: list[dict[str, Any]]) -> bool:
    """STEP 2B (Phase 2): is the whole-goal final tester redundant with the per-phase
    testers that already ran?

    The per-phase coder->tester loop already runs an adversarial tester after each
    phase (``tester:p{idx}r{round}``); a phase only reaches ``status == "passed"``
    once that tester PASSED on the cumulative tree AND cleared the FAIL_TO_PASS gate.
    When EVERY phase passed and NO coder edit has touched the tree since (no forced
    write — the only coder call between the last phase tester and the final verify),
    the ``tester:final`` call would re-run near-identical static checks on a
    byte-identical tree — pure waste (observed in 6/6 traces). Skip it then.

    Conservative by construction: returns False (run the final tester, keeping the
    repair loop intact) whenever any phase failed/blocked, a forced write landed an
    un-reviewed patch, or there are no phase reports. Gated on enforcement, so with
    enforcement OFF this always returns False and the verify path is byte-for-byte
    the reference."""
    if not _enforcement_on(enforcement_strength):
        return False
    if forced or not phase_reports:
        return False
    return all(r.get("status") == "passed" for r in phase_reports)


def _planner_tools(enforcement_strength: str = ENFORCEMENT_OFF) -> list[Any]:
    """Tools for the planning analyst (the PLAN call).

    OFF == reference: returns the exact ``_read_tools()`` list. ON drops bash so
    the planner is confined to read-only file_read + grep — it cannot run shell
    commands or overwrite source via a ``cat >`` redirect (the CRITICAL
    planner-overwrite vector).
    """
    if enforcement_strength == ENFORCEMENT_OFF:
        return _read_tools()
    return [FileReadTool(), GrepTool()]


def _coder_tools(enforcement_strength: str = ENFORCEMENT_OFF) -> list[Any]:
    """Tools for the implement/forced/repair coder calls.

    OFF == reference: returns the exact current 6-tool list AND order. ON drops
    bash (no shell test-theater / find / helper-script creation) and restricts
    file_write to str_replace only (``allow_create=False``), keeping the coder's
    habitual edit path plus apply_patch + run_tests + read/grep.
    """
    if enforcement_strength == ENFORCEMENT_OFF:
        return [
            BashTool(),
            FileReadTool(),
            FileWriteTool(),
            ApplyPatchTool(),
            verification_run_tests_tool(),
            GrepTool(),
        ]
    return [
        FileReadTool(),
        GrepTool(),
        FileWriteTool(allow_create=False),
        ApplyPatchTool(),
        verification_run_tests_tool(),
    ]


def _tester_tools() -> list[Any]:
    return [BashTool(), FileReadTool(), verification_run_tests_tool(), GrepTool()]


def _static_tester_tools() -> list[Any]:
    # No RunTestsTool: where no test runtime exists, run_tests can only waste
    # budget. Static validation needs bash (py_compile), file_read, grep.
    return [BashTool(), FileReadTool(), GrepTool()]


def _tester_prompt(static_verify: bool) -> str:
    return STATIC_TESTER_PROMPT if static_verify else TESTER_PROMPT


def _tester_tools_for(static_verify: bool) -> list[Any]:
    return _static_tester_tools() if static_verify else _tester_tools()


def _time_low(ctx: Any) -> bool:
    """True when the run is within the deadline margin (wall-clock-aware).

    Defensive: a ctx without ``time_low`` (unbounded CLI runs, older test stubs)
    reports False so behavior is unchanged where no deadline is wired.
    """
    time_low = getattr(ctx, "time_low", None)
    return bool(time_low()) if callable(time_low) else False


def _budget_ok(ctx: Any, reserve: int = RESERVE_TOKENS) -> bool:
    """True while there is BOTH enough token budget AND enough wall-clock time
    left for another full coder/tester step.

    ``reserve`` is the headroom that must remain AFTER this step. The implement
    loop uses the default ``RESERVE_TOKENS`` so it always leaves room for a
    forced write plus a final verify; the wrap-up verify/repair pass it ``0`` so
    it runs on whatever the reserve preserved (a forced write capped at
    ``FORCED_WRITE_BUDGET`` cannot eat it all). Bails early once
    ``ctx.time_low()`` reports the hard deadline is near so the reserve is spent
    BEFORE the wall truncates the run (P7 / django-11564 — the edit was located
    but never written because forced-write only checked tokens).
    """
    return ctx.budget.remaining() > reserve and not _time_low(ctx)
