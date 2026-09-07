#!/usr/bin/env python3
"""Parameterised batch scanner that reports discarded runs out loud.

Replaces the hard-coded ``think_scan.py`` (kept untouched next to this file as
a control).  Two things it does that ``think_scan.py`` did not:

1.  It classifies every run as *valid* (the model itself ran to a stop) or
    *invalid*, and **every rate in the output uses only valid runs as its
    denominator**.  ``think_scan.py`` read ``think-decide-first`` as 0/3 while
    two of those three runs were ``APIError`` aborts; the honest number is 0/1.

2.  It always prints how many runs it discarded and why -- including the line
    "discarded 0" when it discarded none.  Without that line "this batch had no
    dead runs" and "the scanner never looked" are the same output, which is the
    exact failure this script exists to prevent.

Usage
-----
    python3 scan_batch.py <batch-dir> [<batch-dir> ...] \
        [--json out.json] [--csv out.csv] [--treat-valid CLASS[,CLASS...]]

Each ``<batch-dir>`` is one cell: a directory holding ``metrics.jsonl`` and,
beside it, the seat snapshots -- in whichever of the three places the arm that
ran keeps them:

    team    logs-team/<instance>/trajectories/*/runtime-*/agent_<aid>_<role>.json
    DW      logs-self-collaboration/<instance>/trajectories/*/runtime-*/<nnn>_<label>.json
    single  <cell>/agent-<hex>/agent.json          (at the batch root; the run's
                                                    own ``trajectory_path`` is
                                                    the only thing naming it)

Reading only the first of those was a silent defect, not an error: on
2026-09-04 every single-arm and every DW run came out ``no_trajectory`` with
all seat columns zero, which is exactly how a run whose seats did nothing
reads, while the cell report's JSON over the same directories had the numbers.
Per-seat quantities are also summed **by role**, because a scripted workflow
seats its analyst twice and three fixed aids print the wrong column.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as _dt
import glob
import json
import os
import re
import sys
from typing import Any

SCHEMA_VERSION = 1

# =============================================================================
# THE VALIDITY CRITERION -- the only place run disposition is hard-coded.
# =============================================================================
#
# Provenance.  Every string below was read off the *writer* side in the local
# checkouts, not inferred from output files:
#
#   run_summary block, key name and field set
#     /root/git/OpenCollab-Eval/src/opencollab_eval/generation/
#         gen_prediction_run_summary.py:30    RUN_SUMMARY_KEY = "run_summary"
#         gen_prediction_run_summary.py:32-39 fields: steps, tokens, status,
#                                             reason, duration_s, error
#         gen_prediction_run_summary.py:21-23 docstring, verbatim: '``status``
#             is the run's terminal disposition as the runtime reported it
#             ("completed" / "stopped" / "failed"), and ``reason`` is that
#             runtime's own detail string'
#
#   where status/reason come from
#     /root/git/OpenCollab/opencollab/bootstrap/programmatic.py:302-311
#         _agent_status(): -> ("failed", terminal_reason or "agent failed")
#                           / ("stopped", "timeout")
#                           / ("stopped", terminal_reason) / ("completed", None)
#     /root/git/OpenCollab/opencollab/bootstrap/programmatic.py:618  reason="timeout"
#     /root/git/OpenCollab/opencollab/bootstrap/programmatic.py:630
#         reason = str(failed_error) or type(failed_error).__name__
#     /root/git/OpenCollab/opencollab/bootstrap/programmatic.py:647-649
#         status = "stopped" if budget_stopped else "completed"
#         reason = "budget_exceeded" if budget_stopped else None
#
#   every graceful-stop reason string, verbatim
#     /root/git/OpenCollab/opencollab/application/session_run.py:563   "wind-down complete: forced commit within reserve"
#     .../session_run.py:574-576  "budget reserve exhausted: protected commit turn already used"
#     .../session_run.py:604-606  "interrupted by user"
#     .../session_run.py:611      "loop block limit reached: {n} repeated tool calls"
#     .../session_run.py:616      "budget exceeded: {n} tokens used"
#     .../session_run.py:625      "team budget exceeded: aggregate spend reached the global cap"
#     .../session_run.py:633      "step limit reached: {n} steps"
#     .../session_run.py:656-660  "budget exhausted before model call: conservative input reservation ..."
#     .../session_run.py:718      "output truncated: provider reached its generation limit"
#     /root/git/OpenCollab/opencollab/application/_session_run_completion.py:619
#                                 "context overflow: prompt exceeds the model context window even after compaction"
#
#   our own lifecycle faults (harness_error), by class / message
#     /root/git/OpenCollab/opencollab/bootstrap/programmatic.py:84   ProgrammaticLifecycleError
#     .../programmatic.py:290-299  "agent trajectory persistence failed"
#     .../programmatic.py:600      "workflow manifest finalization failed"
#     .../programmatic.py:643      "workflow completed without a result"
#     /root/git/OpenCollab/opencollab/sdk/result.py:12,42  RunError("run {status}: {detail}")
#     /root/git/OpenCollab/opencollab/bootstrap/workflow_runtime.py:43,56
#     /root/git/OpenCollab/opencollab/bootstrap/agent_runtime.py:23
#     /root/git/OpenCollab/opencollab/application/scheduler_types.py:17,27,43,61
#
#   provider faults (api_error)
#     /root/git/OpenCollab/opencollab/adapters/llm/responses_errors.py:8-36
#     /root/git/OpenCollab/opencollab/adapters/llm/errors.py:19-27
#     Note: "APIError" itself is the *upstream openai SDK* class name; it does
#     not appear in the OpenCollab tree.  It reaches ``reason`` through the
#     ``f"{type(exc).__name__}: {exc}"`` shape used at
#     /root/git/OpenCollab/opencollab/application/self_collaboration.py:148.
#
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# !!! NOT YET CHECKED AGAINST REAL RECORDS.  gpu3 was unreachable when this !!!
# !!! was written, so every pattern here is source-derived and fixture-      !!!
# !!! tested only.  Before any number from this script goes in the paper,    !!!
# !!! run the positive control in DEFERRED_VERIFICATION at the bottom of     !!!
# !!! this file against                                                      !!!
# !!! ~/oc-team-smoke/think-decide-first and confirm it reports 2 api_error  !!!
# !!! and an alpha denominator of 1.                                         !!!
# !!!                                                                        !!!
# !!! ALSO: this classification is per-QUANTITY, not per-run. See the ruling !!!
# !!! above ALPHA_VALID_CLASSES before reusing it for a new measure.         !!!
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

#: Terminal ``status`` values the runtime can report.
KNOWN_STATUSES = ("completed", "stopped", "failed")

#: The one status that means "the model itself ran to a stop".
VALID_STATUSES = ("completed",)

#: Ordered (class, status, compiled reason pattern) rules.  First match wins, so
#: the specific patterns must precede the general ones.  ``None`` as the pattern
#: means "any reason with this status".  Matching is a case-insensitive search
#: over ``reason`` and, when reason does not match, over ``error``.
_RULES: tuple[tuple[str, str, str | None], ...] = (
    # ---- status == "completed" -------------------------------------------
    ("completed", "completed", None),
    # ---- status == "stopped": controlled halts, in specificity order ------
    ("budget_exhausted", "stopped", r"budget[_ ]exceeded|budget exhausted|budget reserve exhausted"),
    ("provider_truncated", "stopped", r"output truncated: provider reached its generation limit"),
    ("step_limit", "stopped", r"step limit reached"),
    ("loop_block", "stopped", r"loop block limit reached"),
    ("context_overflow", "stopped", r"context overflow"),
    ("wall_clock_timeout", "stopped", r"^timeout$"),
    ("interrupted", "stopped", r"interrupted by user"),
    ("wind_down", "stopped", r"wind-down complete"),
    # ---- status == "failed": ours first, then the provider's --------------
    (
        "harness_error",
        "failed",
        r"ProgrammaticLifecycleError|WorkflowLifecycleError|AgentRuntimeLifecycleError"
        r"|SchedulerStalledError|SchedulerTurnError|TeamPrebuiltError|DuplicateSpawnError"
        r"|SnapshotSessionError|ProcessCleanupError|PendingRowError|RunError"
        r"|trajectory persistence failed|manifest finalization failed"
        r"|completed without a result|did not quiesce|cleanup",
    ),
    (
        "api_error",
        "failed",
        r"APIError|APIStatusError|APIConnectionError|APITimeoutError"
        r"|RateLimitError|InternalServerError|BadRequestError"
        r"|Responses(?:Protocol|TerminalEvent|EmptyOutput|StreamInterrupted|TransientEvent)Error"
        r"|TransientProviderError|TransientEmptyOutputError|StreamedUsageUnavailableError"
        r"|upstream|response stream was interrupted|provider",
    ),
)

_COMPILED: tuple[tuple[str, str, re.Pattern[str] | None], ...] = tuple(
    (cls, status, re.compile(pat, re.IGNORECASE) if pat else None)
    for cls, status, pat in _RULES
)

# ---------------------------------------------------------------------------
# WHICH CLASSES COUNT -- and the ruling that this is decided per *quantity*,
# not per run.  (Coordinator ruling, 2026-09-01.)
# ---------------------------------------------------------------------------
#
# The same run can be a good observation for one quantity and a censored one for
# another, so there is no single "valid run" set.  Two sets:
#
#   ALPHA_VALID_CLASSES -- for the delegation rate alpha.
#       ``budget_exhausted`` counts as VALID here.  A run that burned its whole
#       budget without ever delegating had every chance to delegate and did not:
#       that is real information about delegation propensity, and it is the most
#       informative class we have.  Discarding it would also throw away most of
#       the data -- an earlier batch was 8 budget-stops out of 12 runs.
#       ``provider_truncated`` also counts as valid, and is counted separately.
#       Evidence for both: today's three cap-hitting runs (two budget, one
#       truncated) all produced a patch (1113 / 1091 / 1097 bytes), i.e. the
#       work was substantially done; the two api_error runs produced none.
#
#   OUTCOME_VALID_CLASSES -- for COST ratios.  Only ``completed``.  A run
#       stopped by its token allowance or by the wall clock is RIGHT-CENSORED on
#       spend: its token total is a cap, not a spend, and mixing a cap into a
#       mean silently flattens exactly the quantity being compared.  They are
#       held out of the spend and reported as censored.
#
#       NOT the resolve denominator, corrected 2026-09-07.  The comment here
#       used to say "resolve rate and cost ratios", and for resolve that is
#       wrong about what the paper actually reports: every resolve rate in the
#       paper is resolved / (every instance in the cell) -- 32/40 on the primary
#       rung, 39/50 for Single -- so a run its budget ended is already counted,
#       and counted as unresolved.  That is the right denominator for an
#       equal-budget frame: the budget is the treatment condition, not a
#       nuisance, so "did not deliver inside the budget every arm was given" is
#       the outcome, not missing data.  Censoring resolve would also select on a
#       post-treatment variable that differs by arm -- a Team run opens more
#       seats and meets its budget more often -- which is the same objection
#       that keeps budget stops inside the alpha denominator.  Do not compute a
#       resolve rate from this set.
#
#   api_error / harness_error -- INVALID for every quantity.  The run died
#       mid-flight, so the model never finished the decision process being
#       measured.  Today's two decide-first aborts stopped at 22 and 11 steps
#       with an empty patch.
#
# BEFORE REUSING THIS CLASSIFICATION FOR A NEW QUANTITY, re-read this ruling:
# the sets above are justified by what alpha and what resolve/cost each need,
# and a third quantity may need a third set rather than either of these.

#   ``wall_clock_timeout`` counts as VALID here too, added 2026-09-07 to match
#       the rule the protocol appendix already carried (99b-protocol.tex, D21,
#       committed cd8a894 before the second model's batch reported).  The wall
#       clock is a budget, not a hazard: a run the clock ends had the same
#       allowance every other run had and did not deliver, which is information
#       about the arm, not about the machine.  Reclassifying it as an instrument
#       failure requires evidence the log records -- a container that did not
#       start, a gateway status -- and never an inference from how long the run
#       took.  Two such timing inferences were tried and both failed their
#       control: the wall clock not accounted for by model calls and container
#       commands is a median 4.2%/3.3% here and still only 14.8% on the host we
#       suspected of a slow link, because transport time sits inside the
#       recorded per-call latency; and the cheapest call on that host took
#       2.18 s against 0.74 s for a different model on the same host.
#       Worked case, the only wall-clock censoring on record: 72.6% of its
#       5,459 s went into container commands, and those commands were the run's
#       own polling loops waiting on ``pgrep -f`` against a pattern its own
#       command line contained, so five loops ran their full 840/825/720/550/550
#       seconds.  That is the arm failing to use its budget, not the instrument.
#       It stays censored for OUTCOME (see below): the clock is a cap on that
#       run's spend, so its cost is a bound, and its resolve outcome is held out
#       rather than counted as a failure.  Whether an equal-budget frame should
#       instead score every budget-ended run as unresolved is a separate ruling
#       that would move the already-reported cells, and is NOT taken here.
#
#   MUTATION CHECK, 2026-09-07: removing ``wall_clock_timeout`` from the set
#       below moves qwen-cmdprimary40-r4 from 6/33 back to 5/32 and prints the
#       run as excluded.  Run that before trusting any edit to this set; the
#       fixture the header names (~/oc-team-smoke/think-decide-first) no longer
#       exists on disk, so this is the control that does.

#: Denominator for the delegation rate alpha.
ALPHA_VALID_CLASSES = frozenset(
    {"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout"}
)

#: Denominator for resolve rate and cost ratios: uncensored runs only.
OUTCOME_VALID_CLASSES = frozenset({"completed"})

#: Classes that are valid for alpha but censored for outcomes -- printed as a
#: named hold-out so the two denominators never differ without saying why.
CENSORED_FOR_OUTCOME = frozenset(ALPHA_VALID_CLASSES - OUTCOME_VALID_CLASSES)

#: A run whose status/reason matched no rule.  Never silently valid: it is
#: discarded *and* its raw reason is printed, so a new runtime string shows up
#: as a loud unknown rather than as a quietly shifted denominator.
UNCLASSIFIED = "unclassified"

#: A metrics row with no trajectory on disk.  Also never silently valid.
NO_TRAJECTORY = "no_trajectory"


def classify_run(status: Any, reason: Any, error: Any = None) -> tuple[str, str]:
    """Return ``(validity_class, evidence)`` for one run.

    ``evidence`` is the short string a human needs to check the call by hand.
    """
    s = "" if status is None else str(status).strip().lower()
    r = "" if reason is None else str(reason)
    e = "" if error is None else str(error)
    for cls, want_status, pattern in _COMPILED:
        if s != want_status:
            continue
        if pattern is None:
            return cls, f"status={s!r}"
        for field_name, text in (("reason", r), ("error", e)):
            if text and pattern.search(text):
                return cls, f"status={s!r} {field_name}={text[:160]!r}"
    return UNCLASSIFIED, f"status={s!r} reason={r[:120]!r} error={e[:120]!r}"


#: Ordered ``(class, reason pattern)`` rules for ONE SEAT's ``terminal_reason``.
#: First match wins, so the narrow patterns must precede the wide ones; matching
#: is a case-insensitive search, as in ``_RULES``.
#:
#: Deliberately a SEPARATE table from ``_RULES`` rather than a filtered view of
#: it.  ``_RULES`` dispatches on the run-level ``status`` field ("completed" /
#: "stopped" / "failed"), which a seat record does not carry at all, and reusing
#: it for seats meant consulting only its ``status == "stopped"`` half.  That
#: half is the wrong half: the real records (see ``classify_seat_reason``) show
#: seats carrying the provider faults ``_RULES`` only reaches under
#: ``status == "failed"`` ("InternalServerError: Error code: 503 - ...") and
#: three lifecycle words ("submitted", "completed", "cancelled") that ``_RULES``
#: has no reason string for at all.  Editing ``_RULES`` to cover them would
#: change how RUNS are classified, which is a different question with different
#: consequences for the alpha and outcome denominators.
_SEAT_RULES: tuple[tuple[str, str], ...] = (
    # ---- narrow first: strings a wider rule below would also swallow ------
    # Contains "cancelled", so it must not fall through to the bare
    # ``cancelled`` rule: this names the scheduler tearing down delegated work,
    # which is evidence that delegation HAD happened, not that the seat stopped.
    ("cleanup_cancelled", r"scheduler cleanup cancelled"),
    # Contains "provider", which the wide api_error rule below also matches.
    ("provider_truncated", r"output truncated: provider reached its generation limit"),
    # ---- graceful stops, verbatim from session_run.py --------------------
    ("budget_exhausted", r"budget[_ ]exceeded|budget exhausted|budget reserve exhausted"),
    ("step_limit", r"step limit reached"),
    ("loop_block", r"loop block limit reached"),
    ("context_overflow", r"context overflow"),
    ("wind_down", r"wind-down complete"),
    # "interrupted by user", not the "...stream was interrupted" of api_error.
    ("interrupted", r"interrupted by user"),
    ("wall_clock_timeout", r"^timeout$"),
    # ---- the seat's own lifecycle words, anchored so they stay exact ------
    ("submitted", r"^submitted$"),
    ("completed", r"^completed$"),
    ("cancelled", r"^cancelled$"),
    # ---- provider faults, which reach the seat record verbatim -----------
    # The class names come from the upstream openai SDK via the
    # ``f"{type(exc).__name__}: {exc}"`` shape at
    # /root/git/OpenCollab/opencollab/application/self_collaboration.py:148.
    # The two bare-message alternatives are there so a variant that loses the
    # class-name prefix still lands here instead of going UNCLASSIFIED.
    (
        "api_error",
        r"APIError|APIStatusError|APIConnectionError|APITimeoutError"
        r"|RateLimitError|InternalServerError|BadRequestError"
        r"|Responses(?:Protocol|TerminalEvent|EmptyOutput|StreamInterrupted|TransientEvent)Error"
        r"|TransientProviderError|TransientEmptyOutputError|StreamedUsageUnavailableError"
        r"|concurrency limit exceeded|rate limit exceeded"
        r"|upstream|response stream was interrupted|provider",
    ),
    # A bare "error" and nothing else: the runtime knew the seat died but had no
    # detail to give.  Anchored, so it never swallows a string that names a cause.
    ("seat_error", r"^error$"),
)

_SEAT_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (cls, re.compile(pat, re.IGNORECASE)) for cls, pat in _SEAT_RULES
)

# ---------------------------------------------------------------------------
# WHICH SEAT CLASSES MAKE "THIS RUN DID NOT DELEGATE" AN UNTRUSTWORTHY READING
# ---------------------------------------------------------------------------
#
# Alpha is measured by the ABSENCE of a ``message_agent`` call, and an absence
# only means "chose not to delegate" if the seat was still free to choose.  The
# criterion for the split below is exactly that:
#
#   preemptive -- the seat was cut off by something outside its own decision
#       (its budget ran out, the provider errored, the scheduler cancelled it,
#       the runtime gave up with a bare "error").  A zero delegation count on
#       such a run is censored: the seat may simply never have reached the
#       point where it would have delegated.  Counting it as a refusal to
#       delegate biases alpha DOWNWARD by exactly the amount we are measuring.
#
#   natural end -- the seat reached its own terminal action ("submitted") or
#       ran to the end of its turn budget without being cut ("completed").  It
#       had the whole run to delegate and did not; the observation is good.
#
# ``loop_block`` is in NEITHER set on purpose.  A seat stopped for repeating a
# tool call may have been looping before it ever formed the intent to delegate,
# or after having already delegated and gone back to work.  The record does not
# say which, so it is left undetermined rather than guessed into one of the two
# sets.  n = 11 in the batches to date, so this costs almost nothing either way.
#
# ``interrupted`` and ``wall_clock_timeout`` ARE preemptive, on the same
# criterion as budget_exhausted: both name an outside cut, not a decision. Note
# ``interrupted`` is the third most common seat string on record (184 of 1,673,
# almost all DW runs), so leaving it out of this set would have quietly kept a
# large censored block inside the trustworthy half.

#: Seat classes where the seat was cut off from outside, so a zero delegation
#: count on that run is censored rather than informative.
SEAT_PREEMPTIVE_CLASSES = frozenset({
    "budget_exhausted", "provider_truncated", "cancelled", "cleanup_cancelled",
    "api_error", "seat_error", "interrupted", "wall_clock_timeout",
    "step_limit", "context_overflow", "wind_down",
})

#: Seat classes where the seat reached its own terminal action, so a zero
#: delegation count on that run is a real observation about delegation.
SEAT_NATURAL_END_CLASSES = frozenset({"submitted", "completed"})

#: Seat classes whose position relative to the delegation decision is unknown.
#: Kept out of both sets above rather than guessed into one; see the note.
SEAT_UNDETERMINED_CLASSES = frozenset({"loop_block"})


def classify_seat_reason(reason: Any) -> str | None:
    """Classify ONE SEAT's ``session_state.terminal_reason``.

    Which seat ran out is not the same fact as the run running out.  A run whose
    ``analyst`` seat exhausted its budget and a run whose ``coder`` seat did mean
    opposite things for alpha: in the first the analyst never got to a handover,
    in the second a handover had already happened.  The run-level
    ``budget_exhausted`` label cannot tell those apart, so read the seat too.

    Source for the field: /root/git/OpenCollab/opencollab/application/session.py:
    673-700 -- ``session_state.terminal_reason``, written per agent file.
    Set by /root/git/OpenCollab/opencollab/application/session_run.py:537 via
    ``_stop_precheck``.

    Matched against ``_SEAT_RULES``, which is the seat's own table; see the
    comment there for why it is not a filtered view of ``_RULES``.  Returns
    ``None`` when there is no reason at all (an empty string is what a seat that
    never stopped that way writes) and ``UNCLASSIFIED`` for an unseen string.

    VERIFIED 2026-09-06 against 1,673 seat records -- every seat of all 689 runs
    in the 26 ``/root/oc-batches/*.report.json`` batch reports, read from
    ``runs[].seats[].terminal``, which is this same field.  Those records hold 16
    distinct strings once the numeric tails of ``budget exhausted before model
    call: ...`` and of the 503 / 429 provider payloads are folded together.  This
    table classifies all 16; none is left ``UNCLASSIFIED``:

        (empty) -> None      542
        submitted            624   analyst 313 / coder 146 / tester 143 /
                                   swe_agent 22
        interrupted          184   "interrupted by user"
        budget_exhausted     156   "budget exhausted before model call: ..."
        api_error             84   503 x41, "Concurrency limit exceeded for
                                   user" x30, 429 x6, "Upstream response stream
                                   was interrupted" x4, "Upstream HTTP/2 stream
                                   failed" x2, "Upstream request failed" x1
        completed             36
        seat_error            16   a bare "error"
        cancelled             13
        loop_block            11   "loop block limit reached: 3 repeated tool
                                   calls"
        provider_truncated     4
        cleanup_cancelled      3   "Error: scheduler cleanup cancelled
                                   delegated work"

    What this verification overturned: reading only the ``status == "stopped"``
    half of ``_RULES`` matched 3 of those 16 families (budget_exhausted,
    loop_block, provider_truncated, 171 records).  The other 11 non-empty
    families, 960 records, went ``UNCLASSIFIED`` -- including ``submitted``,
    which is the single most common seat string on record.

    Not seen in those 1,673 records, so still source-derived only:
    ``step_limit``, ``context_overflow``, ``wind_down``, ``wall_clock_timeout``.
    """
    if reason is None or str(reason).strip() == "":
        return None
    text = str(reason)
    for cls, pattern in _SEAT_COMPILED:
        if pattern.search(text):
            return cls
    return UNCLASSIFIED


# =============================================================================
# Trajectory measurements
# =============================================================================

WRITE_TOOLS = frozenset({"apply_patch", "file_write"})

#: Shell fragments that write.  Same regex ``think_scan.py`` used, so the two
#: scanners stay comparable on this column.
BASH_WRITE_RE = re.compile(r"(>\s*[^&\s]|>>|open\([^)]*['\"][wa]|sed -i|tee |cat\s*<<|dd of=)")

#: Teammate-reference terms counted inside aid=0 reasoning text.
TEAMMATE_TERMS = (
    "coder", "tester", "delegat", "teammate", "hand off", "hand it over",
    "message_agent", "team_status", "colleague",
)

#: Control terms that must be present if the reasoning text was captured at all.
#: A zero teammate count next to a zero control count means "no reasoning text",
#: not "the model never mentioned its teammates".
REASONING_CONTROL_TERMS = ("test", "patch", "fix")

#: Keys the model's reasoning text has been seen under.
REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


#: How each arm names the per-seat snapshot the runtime autosaves, relative to
#: the directory that holds one run's seats. A team writes
#: ``agent_<aid>_<role>-<hex>.json``; a scripted workflow writes
#: ``<nnn>_<label>.json`` (``000_analyst.json``, ``003_analyst-adjudicate-r1``),
#: which the team pattern does not match. The journal sidecars
#: (``*.json.journal``) match neither, which is why neither has to exclude them.
#: Source for the layouts: /root/git/OpenCollab/opencollab/bootstrap/
#: session_factory.py:99-106 and the DW runs under
#: ``logs-self-collaboration/<instance>/trajectories/*/runtime-*/``.
SEAT_FILE_GLOBS = ("agent_*.json", "[0-9][0-9][0-9]_*.json")

#: The single-agent arm keeps one directory per run at the *batch root*,
#: ``<cell>/agent-<hex>/agent.json``, named by a random id that carries no
#: instance. No glob under ``logs-single/<instance>/`` finds it, which is why
#: every single-arm run read as ``no_trajectory``; the only thing tying the
#: directory to the instance is the run's own ``trajectory_path``.
SINGLE_SEAT_FILE = "agent.json"

#: The role the runtime stamps on every seat of a scripted workflow. A seat
#: that carries it has its identity in its file name instead.
GENERIC_WORKFLOW_ROLE = "workflow_agent"


def _aid_from_filename(path: str) -> int | None:
    """The seat's id from its file name, in either arm's spelling.

    ``agent_<aid>_<role>.json`` (team) and ``<nnn>_<label>.json`` (workflow).
    Source for the layout: /root/git/OpenCollab/opencollab/bootstrap/
    session_factory.py:99-106 (``agent_save_path``).
    """
    base = os.path.basename(path)
    m = re.match(r"agent_(\d+)_", base) or re.match(r"^(\d{3})_", base)
    return int(m.group(1)) if m else None


def _role_from_filename(path: str) -> str | None:
    """A workflow seat's real role, from the name the driver gave its file.

    ``001_coder-r1`` is the coder; ``003_analyst-adjudicate-r1`` is the analyst
    coming back to adjudicate. Without this every DW seat reads as
    ``workflow_agent`` and a per-role column cannot be built at all.
    """
    stem = os.path.basename(path).rsplit(".", 1)[0]
    label = stem.split("_", 1)[1] if "_" in stem else stem
    return label.split("-", 1)[0] or None


def _reasoning_text(message: dict) -> str:
    for key in REASONING_KEYS:
        value = message.get(key)
        if value:
            return str(value)
    return ""


def empty_measurements() -> dict[str, Any]:
    """Zeroed measurement block, so a run with no trajectory still has every key.

    A missing key and a zero must not be the same thing downstream: the run
    carries ``trajectory_found=False`` to say which one this is.
    """
    return {
        "message_agent_calls": 0,
        "message_agent_by_aid": {},
        "team_status_calls": 0,
        "team_status_by_aid": {},
        "write_tool_calls_by_aid": {},
        "bash_write_calls_by_aid": {},
        "tool_calls_by_name": {},
        "tokens_by_aid": {},
        "steps_by_aid": {},
        "assistant_msgs_by_aid": {},
        "roles_by_aid": {},
        # Summed over the seats that held each role, because a scripted
        # workflow seats its analyst twice and a per-aid column cannot be
        # compared with the cell report, which reports per role.
        "tokens_by_role": {},
        "assistant_msgs_by_role": {},
        "seats_by_role": {},
        "seat_phase_by_aid": {},
        "seat_terminal_reason_by_aid": {},
        "seat_terminal_class_by_aid": {},
        "budget_exhausted_aids": [],
        "models": [],
        "reasoning_chars_aid0": 0,
        "reasoning_teammate_hits": 0,
        "reasoning_teammate_hits_by_term": {},
        "reasoning_control_hits": 0,
        "n_agent_files": 0,
    }


def measure_trajectory(runtime_dir: str) -> dict[str, Any]:
    """Per-run mechanism quantities from one ``runtime-*`` directory.

    agent_*.json schema source: /root/git/OpenCollab/opencollab/application/
    session.py:673-700 (``_snapshot_for_save``) -- top level ``aid``, ``role``,
    ``model``, ``session_state`` (``used_tokens``, ``step_count``, ``phase``,
    ``terminal_reason``), plus ``messages`` appended by
    /root/git/OpenCollab/opencollab/adapters/storage.py:129.
    """
    out: dict[str, Any] = empty_measurements()
    calls_by_name: collections.Counter[str] = collections.Counter()
    msg_by_aid: collections.Counter[int] = collections.Counter()
    ts_by_aid: collections.Counter[int] = collections.Counter()
    write_by_aid: collections.Counter[int] = collections.Counter()
    bashw_by_aid: collections.Counter[int] = collections.Counter()
    asst_by_aid: collections.Counter[int] = collections.Counter()
    term_hits: collections.Counter[str] = collections.Counter()
    models: set[str] = set()

    tokens_by_role: collections.Counter[str] = collections.Counter()
    asst_by_role: collections.Counter[str] = collections.Counter()
    seats_by_role: collections.Counter[str] = collections.Counter()

    seat_files: set[str] = set()
    for pattern in SEAT_FILE_GLOBS:
        seat_files.update(glob.glob(os.path.join(runtime_dir, pattern)))
    single = os.path.join(runtime_dir, SINGLE_SEAT_FILE)
    if os.path.isfile(single):
        seat_files.add(single)
    for agent_file in sorted(seat_files):
        try:
            with open(agent_file, encoding="utf-8") as handle:
                agent = json.load(handle)
        except (OSError, ValueError):
            continue
        out["n_agent_files"] += 1
        aid = agent.get("aid")
        if aid is None:
            aid = _aid_from_filename(agent_file)
        if aid is None:
            continue
        aid = int(aid)
        recorded_role = str(agent.get("role") or "")
        role = (
            recorded_role
            if recorded_role and recorded_role != GENERIC_WORKFLOW_ROLE
            else (_role_from_filename(agent_file) or recorded_role)
        )
        if role:
            out["roles_by_aid"][aid] = role
            seats_by_role[role] += 1
        if agent.get("model"):
            models.add(str(agent["model"]))
        state = agent.get("session_state") or {}
        out["tokens_by_aid"][aid] = int(state.get("used_tokens") or 0)
        out["steps_by_aid"][aid] = int(state.get("step_count") or 0)
        if role:
            tokens_by_role[role] += int(state.get("used_tokens") or 0)
        if state.get("phase"):
            out["seat_phase_by_aid"][aid] = state.get("phase")
        if state.get("terminal_reason"):
            out["seat_terminal_reason_by_aid"][aid] = state.get("terminal_reason")
            seat_class = classify_seat_reason(state.get("terminal_reason"))
            if seat_class is not None:
                out["seat_terminal_class_by_aid"][aid] = seat_class
                if seat_class == "budget_exhausted":
                    out["budget_exhausted_aids"].append(aid)

        for message in agent.get("messages") or []:
            if message.get("role") == "assistant":
                asst_by_aid[aid] += 1
                if role:
                    asst_by_role[role] += 1
            reasoning = _reasoning_text(message)
            if reasoning and aid == 0:
                out["reasoning_chars_aid0"] += len(reasoning)
                lowered = reasoning.lower()
                for term in TEAMMATE_TERMS:
                    hits = lowered.count(term)
                    if hits:
                        term_hits[term] += hits
                for term in REASONING_CONTROL_TERMS:
                    out["reasoning_control_hits"] += lowered.count(term)
            for call in message.get("tool_calls") or []:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                calls_by_name[name] += 1
                if name == "message_agent":
                    msg_by_aid[aid] += 1
                elif name == "team_status":
                    ts_by_aid[aid] += 1
                elif name in WRITE_TOOLS:
                    write_by_aid[aid] += 1
                elif name == "bash":
                    raw = fn.get("arguments") or ""
                    command = ""
                    try:
                        command = json.loads(raw).get("command", "") if raw else ""
                    except (ValueError, AttributeError):
                        command = str(raw)
                    if BASH_WRITE_RE.search(str(command)):
                        bashw_by_aid[aid] += 1

    out["message_agent_calls"] = int(sum(msg_by_aid.values()))
    out["message_agent_by_aid"] = dict(sorted(msg_by_aid.items()))
    out["team_status_calls"] = int(sum(ts_by_aid.values()))
    out["team_status_by_aid"] = dict(sorted(ts_by_aid.items()))
    out["write_tool_calls_by_aid"] = dict(sorted(write_by_aid.items()))
    out["bash_write_calls_by_aid"] = dict(sorted(bashw_by_aid.items()))
    out["assistant_msgs_by_aid"] = dict(sorted(asst_by_aid.items()))
    out["tokens_by_role"] = dict(sorted(tokens_by_role.items()))
    out["assistant_msgs_by_role"] = dict(sorted(asst_by_role.items()))
    out["seats_by_role"] = dict(sorted(seats_by_role.items()))
    out["tool_calls_by_name"] = dict(sorted(calls_by_name.items()))
    out["reasoning_teammate_hits_by_term"] = dict(sorted(term_hits.items()))
    out["reasoning_teammate_hits"] = int(sum(term_hits.values()))
    out["budget_exhausted_aids"] = sorted(out["budget_exhausted_aids"])
    out["models"] = sorted(models)
    return out


# =============================================================================
# Cell scanning
# =============================================================================

def _find_runtime_dir(cell_dir: str, instance_id: str, record: dict | None = None) -> str | None:
    """The directory holding one run's seat snapshots, in whichever arm wrote it.

    Located by the seats themselves rather than by ``team.json``: only the team
    arm writes that file, so looking for it read every DW and every single-arm
    run as ``no_trajectory`` -- with every seat column zero, which is also how a
    run whose seats did nothing reads.

    The single arm is the one case a glob cannot answer. Its directory sits at
    the batch root under a random id (``agent-<hex>``) that names no instance,
    so the tie has to come from the run's own ``trajectory_path``. That path was
    written on the machine that ran the batch, so it is resolved by name under
    this cell first and taken verbatim only when the batch is read where it ran.
    """
    seat_patterns = [os.path.join(cell_dir, "logs-*", instance_id, "**", "team.json")]
    seat_patterns += [
        os.path.join(cell_dir, "logs-*", instance_id, "**", glob_pattern)
        for glob_pattern in SEAT_FILE_GLOBS
    ]
    seat_patterns.append(os.path.join(cell_dir, "**", instance_id, "**", "team.json"))
    for pattern in seat_patterns:
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return os.path.dirname(hits[-1])
    raw = str((record or {}).get("trajectory_path") or "")
    if raw:
        named = os.path.basename(raw.rstrip("/"))
        for directory in (os.path.join(cell_dir, named), raw):
            if os.path.isfile(os.path.join(directory, SINGLE_SEAT_FILE)):
                return directory
    return None


def _read_metrics(cell_dir: str) -> tuple[list[dict], list[str]]:
    """Every ``metrics.jsonl`` row, plus complaints about unreadable ones.

    The metrics file is the spine on purpose.  Scanning by ``team.json`` instead
    -- which ``think_scan.py`` did -- cannot see a run that died before it wrote
    a trajectory, and an invisible run is exactly a silently shrunk denominator.
    """
    path = os.path.join(cell_dir, "metrics.jsonl")
    rows: list[dict] = []
    problems: list[str] = []
    if not os.path.exists(path):
        return rows, [f"{path}: missing"]
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                problems.append(f"{path}:{lineno}: unparseable ({exc})")
                continue
            record["_source_line"] = lineno
            rows.append(record)
    return rows, problems


def scan_cell(
    cell_dir: str,
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> dict[str, Any]:
    """Scan one cell, carrying BOTH denominators.

    ``alpha_valid`` and ``outcome_valid`` are separate on purpose; see the
    ruling above ``ALPHA_VALID_CLASSES``.  Every per-class count is also
    returned, so any third denominator can be recomputed downstream without
    re-running this.
    """
    cell_dir = os.path.abspath(os.path.expanduser(cell_dir))
    name = os.path.basename(cell_dir.rstrip("/"))
    rows, problems = _read_metrics(cell_dir)

    runs: list[dict[str, Any]] = []
    seen: collections.Counter[str] = collections.Counter()
    for record in rows:
        instance_id = record.get("instance_id") or "?"
        seen[instance_id] += 1
        summary = record.get("run_summary") or {}
        status = summary.get("status")
        reason = summary.get("reason")
        error = summary.get("error") if summary.get("error") is not None else record.get("error")
        klass, evidence = classify_run(status, reason, error)

        runtime_dir = _find_runtime_dir(cell_dir, instance_id, record)
        if runtime_dir is None and klass in (alpha_valid | outcome_valid):
            # A run the runtime called complete but that left no trajectory is
            # not a usable observation; say so rather than count it at zero.
            klass, evidence = NO_TRAJECTORY, f"{evidence} (no seat snapshot under {cell_dir})"

        run: dict[str, Any] = {
            "cell": name,
            "cell_path": cell_dir,
            "instance_id": instance_id,
            "metrics_line": record.get("_source_line"),
            "validity_class": klass,
            "valid_for_alpha": klass in alpha_valid,
            "valid_for_outcome": klass in outcome_valid,
            "censored_for_outcome": klass in alpha_valid and klass not in outcome_valid,
            "validity_evidence": evidence,
            "status": status,
            "reason": reason,
            "error": None if error is None else str(error)[:400],
            "steps": summary.get("steps"),
            "tokens": summary.get("tokens"),
            "duration_s": summary.get("duration_s"),
            "patch_chars": record.get("submitted_patch_chars"),
            "trajectory_dir": runtime_dir,
            "trajectory_found": runtime_dir is not None,
        }
        run.update(measure_trajectory(runtime_dir) if runtime_dir else empty_measurements())
        run["delegated"] = run["message_agent_calls"] > 0
        runs.append(run)

    for instance_id, count in seen.items():
        if count > 1:
            problems.append(
                f"{os.path.join(cell_dir, 'metrics.jsonl')}: instance_id {instance_id!r} "
                f"appears {count} times; all {count} rows are kept as separate runs"
            )

    a_valid = [r for r in runs if r["valid_for_alpha"]]
    a_dropped = [r for r in runs if not r["valid_for_alpha"]]
    o_valid = [r for r in runs if r["valid_for_outcome"]]
    o_censored = [r for r in runs if r["censored_for_outcome"]]
    o_dropped = [r for r in runs if not r["valid_for_outcome"] and not r["censored_for_outcome"]]

    def _by_class(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(sorted(collections.Counter(r["validity_class"] for r in rows).items()))

    delegating_alpha = [r for r in a_valid if r["delegated"]]
    delegating_all = [r for r in runs if r["delegated"]]

    # Which seat ran out, aggregated -- see classify_seat_reason.
    seat_budget = collections.Counter()
    for r in runs:
        for aid in r.get("budget_exhausted_aids") or []:
            seat_budget[aid] += 1

    return {
        "cell": name,
        "cell_path": cell_dir,
        "n_records": len(runs),
        # Every class, every time: the raw material for any other denominator.
        "class_counts": _by_class(runs),

        # ---- denominator 1: alpha (budget/truncated runs KEPT) ------------
        "alpha_valid_classes": sorted(alpha_valid),
        "n_valid_alpha": len(a_valid),
        "n_dropped_alpha": len(a_dropped),
        "dropped_alpha_by_class": _by_class(a_dropped),
        "dropped_alpha_runs": [
            {
                "instance_id": r["instance_id"],
                "metrics_line": r["metrics_line"],
                "validity_class": r["validity_class"],
                "validity_evidence": r["validity_evidence"],
            }
            for r in a_dropped
        ],
        "alpha_num": len(delegating_alpha),
        "alpha_den": len(a_valid),
        "alpha": (len(delegating_alpha) / len(a_valid)) if a_valid else None,
        # alpha the old way (every metrics row in the denominator), printed
        # beside it so a changed cell is visible instead of silently corrected.
        "alpha_all_num": len(delegating_all),
        "alpha_all_den": len(runs),
        "alpha_all": (len(delegating_all) / len(runs)) if runs else None,

        # ---- denominator 2: resolve rate / cost (uncensored only) ---------
        "outcome_valid_classes": sorted(outcome_valid),
        "n_valid_outcome": len(o_valid),
        "n_censored_outcome": len(o_censored),
        "censored_outcome_by_class": _by_class(o_censored),
        "censored_outcome_runs": [
            {
                "instance_id": r["instance_id"],
                "metrics_line": r["metrics_line"],
                "validity_class": r["validity_class"],
                "tokens": r["tokens"],
                "patch_chars": r["patch_chars"],
            }
            for r in o_censored
        ],
        "n_dropped_outcome": len(o_dropped),
        "dropped_outcome_by_class": _by_class(o_dropped),
        "tokens_outcome": [r["tokens"] for r in o_valid],
        "patch_chars_outcome": [r["patch_chars"] for r in o_valid],
        # The censored side kept separately, never merged into the line above.
        "tokens_censored": [r["tokens"] for r in o_censored],
        "patch_chars_censored": [r["patch_chars"] for r in o_censored],

        # ---- mechanism quantities, on the alpha denominator ---------------
        "message_agent_calls_alpha": sum(r["message_agent_calls"] for r in a_valid),
        "team_status_calls_alpha": sum(r["team_status_calls"] for r in a_valid),
        "reasoning_chars_aid0_alpha": [r["reasoning_chars_aid0"] for r in a_valid],
        "reasoning_teammate_hits_alpha": sum(r["reasoning_teammate_hits"] for r in a_valid),
        "reasoning_control_hits_alpha": sum(r["reasoning_control_hits"] for r in a_valid),
        "budget_exhausted_seat_counts": dict(sorted(seat_budget.items())),

        "problems": problems,
        "runs": runs,
    }


# =============================================================================
# Reporting
# =============================================================================

def _fmt_rate(num: int, den: int, value: float | None) -> str:
    if value is None:
        return f"{num}/{den} (n/a)"
    return f"{num}/{den} = {value:.2f}"


def render_text(
    cells: list[dict[str, Any]],
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 108)
    add("批次扫描 —— 有效性按「要算哪个量」定，不是按 run 定（裁决 2026-09-01）")
    add(f"  α（委派率）分母 valid 类: {sorted(alpha_valid)}")
    add(f"  resolve/成本 分母 valid 类: {sorted(outcome_valid)}  （其余为右截尾，单列不混算）")
    add("=" * 108)

    # ---- 1. 剔除报告 -----------------------------------------------------
    # Printed first and unconditionally, including the zero case: without the
    # zero line "no dead runs" is indistinguishable from "never checked".
    add("")
    add("--- 剔除报告 A：α 的分母（每格必打印，零剔除也打印）---")
    for cell in cells:
        if cell["n_dropped_alpha"] == 0:
            add(f"{cell['cell']:<26s} 剔除 0 个 —— {cell['n_records']} 个 run 全部进 α 分母")
        else:
            breakdown = ", ".join(f"{k}={v}" for k, v in cell["dropped_alpha_by_class"].items())
            add(
                f"{cell['cell']:<26s} 剔除 {cell['n_dropped_alpha']} 个 / 共 "
                f"{cell['n_records']} 个 ({breakdown})"
            )
            for bad in cell["dropped_alpha_runs"]:
                add(
                    f"{'':<26s}   - {bad['instance_id']:<38s} "
                    f"[{bad['validity_class']}] metrics.jsonl:{bad['metrics_line']} "
                    f"{bad['validity_evidence']}"
                )
    add(
        f"{'合计':<26s} 剔除 {sum(c['n_dropped_alpha'] for c in cells)} 个 / 共 "
        f"{sum(c['n_records'] for c in cells)} 个 run"
    )

    add("")
    add("--- 剔除报告 B：resolve/成本 的分母（截尾与剔除分开报）---")
    for cell in cells:
        censored = ", ".join(f"{k}={v}" for k, v in cell["censored_outcome_by_class"].items())
        dropped = ", ".join(f"{k}={v}" for k, v in cell["dropped_outcome_by_class"].items())
        add(
            f"{cell['cell']:<26s} 可用 {cell['n_valid_outcome']}/{cell['n_records']}"
            f"；右截尾 {cell['n_censored_outcome']} 个"
            f"{' (' + censored + ')' if censored else ''}"
            f"；剔除 {cell['n_dropped_outcome']} 个"
            f"{' (' + dropped + ')' if dropped else ''}"
        )
        for row in cell["censored_outcome_runs"]:
            add(
                f"{'':<26s}   ~ {row['instance_id']:<38s} [{row['validity_class']}] "
                f"token={row['tokens']} patch={row['patch_chars']} "
                f"(token 是上限不是花费；patch 非空说明活基本干完了)"
            )

    # ---- 2. alpha 表, new denominator beside old -------------------------
    add("")
    add("--- 委派率 α（新口径 = 含 budget/truncated；旧口径 = 全部 run）---")
    add(f"{'格':<26s} {'α分母/全部':>12s} {'α(valid)':>14s} {'α(旧, 全部)':>16s}  变了?")
    for cell in cells:
        changed = (
            "变了"
            if (cell["alpha_den"] != cell["alpha_all_den"]
                or cell["alpha_num"] != cell["alpha_all_num"])
            else ""
        )
        add(
            f"{cell['cell']:<26s} "
            f"{str(cell['alpha_den']) + '/' + str(cell['n_records']):>12s} "
            f"{_fmt_rate(cell['alpha_num'], cell['alpha_den'], cell['alpha']):>14s} "
            f"{_fmt_rate(cell['alpha_all_num'], cell['alpha_all_den'], cell['alpha_all']):>16s}"
            f"  {changed}"
        )

    # ---- 3. outcome denominator, stated separately -----------------------
    add("")
    add("--- resolve/成本 可用样本（右截尾单列，绝不并入均值）---")
    add(f"{'格':<26s} {'未截尾/全部':>12s} {'未截尾 token':>28s} {'截尾 token(=上限)':>28s}")
    for cell in cells:
        def _brief(values: list[Any]) -> str:
            nums = [v for v in values if isinstance(v, int | float)]
            if not nums:
                return "-"
            return f"n={len(nums)} 均值={sum(nums) / len(nums):,.0f}"
        add(
            f"{cell['cell']:<26s} "
            f"{str(cell['n_valid_outcome']) + '/' + str(cell['n_records']):>12s} "
            f"{_brief(cell['tokens_outcome']):>28s} "
            f"{_brief(cell['tokens_censored']):>28s}"
        )

    # ---- 4. per-run detail ----------------------------------------------
    add("")
    add("--- 逐 run 明细（A=进α分母, O=进resolve/成本分母, ~=右截尾, x=剔除）---")
    for cell in cells:
        for run in cell["runs"]:
            if run["valid_for_outcome"]:
                mark = "AO"
            elif run["censored_for_outcome"]:
                mark = "A~"
            else:
                mark = "xx"
            # By role, summed over the seats that held it, and only the roles
            # this run actually seated. Three fixed aids printed the wrong
            # column on every arm but the team one: a DW analyst is seated
            # twice (aids 0 and 3, so aid 3 fell off the end) and the single
            # arm's only seat is aid -1 (so all three columns read zero).
            seats = " ".join(f"{r}={n:,}" for r, n in run["assistant_msgs_by_role"].items()) or "-"
            tokens = " ".join(f"{r}={n:,}" for r, n in run["tokens_by_role"].items()) or "-"
            burnt = run.get("budget_exhausted_aids") or []
            burnt_note = f" 烧光座位={burnt}" if burnt else ""
            add(
                f"[{mark}] {cell['cell']:<22s} {run['instance_id'][:34]:<34s} "
                f"{run['validity_class']:<19s} "
                f"座位msg={seats:<28s} 座位token={tokens:<48s} "
                f"msg_agent={run['message_agent_calls']:<3d} ts={run['team_status_calls']:<3d} "
                f"写={str(run['write_tool_calls_by_aid']):<12s} "
                f"bash写={str(run['bash_write_calls_by_aid']):<12s} "
                f"reason_chars={run['reasoning_chars_aid0']:<7d} "
                f"队友词={run['reasoning_teammate_hits']:<4d} "
                f"(正控 test/patch/fix={run['reasoning_control_hits']}) "
                f"patch={run['patch_chars']}{burnt_note}"
            )

    # ---- 5. which seat ran out -------------------------------------------
    add("")
    add("--- 预算烧光发生在哪个座位（aid 0=analyst；analyst 烧光与 coder 烧光对 α 含义相反）---")
    any_seat = False
    for cell in cells:
        counts = cell["budget_exhausted_seat_counts"]
        if counts:
            any_seat = True
            add(f"{cell['cell']:<26s} {counts}")
    if not any_seat:
        add("无座位记录到 budget 类 terminal_reason（注意：这也可能是该字段没落盘，尚未核对）")

    # ---- 6. problems -----------------------------------------------------
    add("")
    add("--- 解析问题（空 = 无）---")
    any_problem = False
    for cell in cells:
        for problem in cell["problems"]:
            any_problem = True
            add(f"{cell['cell']:<26s} {problem}")
    if not any_problem:
        add("无")
    return "\n".join(lines)


CSV_FIELDS = (
    "cell", "instance_id", "metrics_line", "validity_class",
    "valid_for_alpha", "valid_for_outcome", "censored_for_outcome",
    "status", "reason", "steps", "tokens", "patch_chars",
    "message_agent_calls", "team_status_calls", "delegated",
    "reasoning_chars_aid0", "reasoning_teammate_hits", "reasoning_control_hits",
    "trajectory_found", "trajectory_dir",
)


def write_csv(path: str, cells: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for cell in cells:
            for run in cell["runs"]:
                writer.writerow(run)


def build_document(
    cells: list[dict[str, Any]],
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        # Two denominators, named, so a downstream reader can never pick one up
        # thinking it is the other.
        "alpha_valid_classes": sorted(alpha_valid),
        "outcome_valid_classes": sorted(outcome_valid),
        "censored_for_outcome_classes": sorted(alpha_valid - outcome_valid),
        "criterion_verified_against_real_records": False,
        "criterion_verification_note": (
            "Source-derived from the OpenCollab / OpenCollab-Eval writers and "
            "fixture-tested only; gpu3 was unreachable. Re-run the positive "
            "control on think-decide-first before publishing any number. "
            "Validity is per-quantity (coordinator ruling 2026-09-01): "
            "budget_exhausted is valid for alpha and censored for resolve/cost."
        ),
        "totals": {
            "n_records": sum(c["n_records"] for c in cells),
            "n_valid_alpha": sum(c["n_valid_alpha"] for c in cells),
            "n_dropped_alpha": sum(c["n_dropped_alpha"] for c in cells),
            "n_valid_outcome": sum(c["n_valid_outcome"] for c in cells),
            "n_censored_outcome": sum(c["n_censored_outcome"] for c in cells),
        },
        "cells": [{k: v for k, v in c.items() if k != "runs"} for c in cells],
        "runs": [r for c in cells for r in c["runs"]],
    }

# =============================================================================
# DEFERRED VERIFICATION -- run this the moment gpu3 is reachable again.
# =============================================================================
DEFERRED_VERIFICATION = """
gpu3 was down when this scanner was written, so the validity criterion above is
source-derived and fixture-tested only.  Nothing from it should go in the paper
until this positive control passes.

Why this cell and not a clean one: a clean batch cannot tell you whether the
dead-run detector works, because "no dead runs" and "detector broken" produce
identical output there.  think-decide-first is the one cell known to contain
dead runs, so it is the only cell where a pass means anything.

  # 1. copy the scanner over (it is stdlib-only, no install needed)
  scp oc-iclr2027/05-experiment/scripts/scan_batch.py gpu3:/tmp/scan_batch.py

  # 2. the positive control
  ssh gpu3 'python3 /tmp/scan_batch.py ~/oc-team-smoke/think-decide-first'

EXPECT, and treat any deviation as the measurement winning over this comment:
  * section A: "剔除 2 个 / 共 3 个 (api_error=2)"
  * the two named runs are the ones at metrics.jsonl lines 2 and 3
  * the alpha row reads  1/3  ...  alpha(valid) = 0/1
    beside alpha(旧, 全部) = 0/3, flagged 变了
  * section B: the same cell shows 可用 1/3, 右截尾 0, 剔除 2

Also check the seat readout on a cell that DID hit its cap
(think-cmd-optout / think-opt-out-message / starved-nudgeoff, all on
pydicom-1031).  Expect "预算烧光发生在哪个座位" to name aid 0, and expect the
censored rows to show a non-empty patch (~1113 / 1091 / 1097 bytes).  If the
seat line instead says "无座位记录到 budget 类 terminal_reason", find out whether
session_state.terminal_reason is actually being persisted before concluding
anything from it -- an empty readout there is not evidence of no burnout.
  * the printed reason string for both is checked by eye against
    ~/oc-team-smoke/think-decide-first/logs-team/pydicom__pydicom-1031/driver.log

IF THE REASON STRINGS DIFFER from the patterns in _RULES, do not widen a
pattern to make the number come out right.  Print the raw values first:

  ssh gpu3 'python3 -c "
import json,sys
for i,l in enumerate(open(sys.argv[1]),1):
    d=json.loads(l); r=d.get(\"run_summary\") or {}
    print(i, d.get(\"instance_id\"), repr(r.get(\"status\")), repr(r.get(\"reason\")), repr(r.get(\"error\"))[:200])
" ~/oc-team-smoke/think-decide-first/metrics.jsonl'

  # and confirm the enumeration across every cell at once, so an unseen
  # runtime string shows up as `unclassified` rather than as a shifted rate:
  ssh gpu3 'python3 /tmp/scan_batch.py ~/oc-team-smoke/think-* \
      ~/oc-team-smoke/starved-nudge* ~/oc-team-smoke/facts-v2-rep* \
      --json /tmp/scan.json' | tail -40

Then set criterion_verified_against_real_records to True in build_document, and
only then quote a number.
"""

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan experiment batch cells; report discarded runs explicitly."
    )
    parser.add_argument("batch_dirs", nargs="+", help="one or more cell directories")
    parser.add_argument("--json", dest="json_out", help="write the machine-readable JSON here")
    parser.add_argument("--csv", dest="csv_out", help="write one row per run here")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "sensitivity check in the conservative direction: also drop "
            "budget_exhausted and provider_truncated from the alpha denominator, "
            "making it identical to the resolve/cost denominator"
        ),
    )
    parser.add_argument(
        "--treat-valid",
        default="",
        help=(
            "comma-separated validity classes to additionally count in the alpha "
            "denominator, for sensitivity in the permissive direction"
        ),
    )
    args = parser.parse_args(argv)

    extra = {c.strip() for c in args.treat_valid.split(",") if c.strip()}
    alpha_valid = frozenset(
        (OUTCOME_VALID_CLASSES if args.strict else ALPHA_VALID_CLASSES) | extra
    )
    outcome_valid = OUTCOME_VALID_CLASSES

    cells = [scan_cell(d, alpha_valid, outcome_valid) for d in args.batch_dirs]
    print(render_text(cells, alpha_valid, outcome_valid))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                build_document(cells, alpha_valid, outcome_valid),
                handle, ensure_ascii=False, indent=2,
            )
        print(f"\nJSON -> {args.json_out}")
    if args.csv_out:
        write_csv(args.csv_out, cells)
        print(f"CSV  -> {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
