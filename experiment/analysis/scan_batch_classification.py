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

import re
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
#     /root/git/OpenCollab/opencollab/application/session_run.py:563   "wind-down complete: forced commit within
# reserve"
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
    (cls, status, re.compile(pat, re.IGNORECASE) if pat else None) for cls, status, pat in _RULES
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
ALPHA_VALID_CLASSES = frozenset({"completed", "budget_exhausted", "provider_truncated", "wall_clock_timeout"})

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
SEAT_PREEMPTIVE_CLASSES = frozenset(
    {
        "budget_exhausted",
        "provider_truncated",
        "cancelled",
        "cleanup_cancelled",
        "api_error",
        "seat_error",
        "interrupted",
        "wall_clock_timeout",
        "step_limit",
        "context_overflow",
        "wind_down",
    }
)

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
