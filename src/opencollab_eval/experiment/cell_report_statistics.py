"""Attempt selection and cell statistics over persisted run rows."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from math import exp, fsum, lgamma, log, log1p
from pathlib import Path
from typing import Any

from opencollab_eval.experiment.cell_report_rows import RunRow


def binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n or p == 0.0:
        return 1.0
    if p == 1.0:
        return 0.0
    # Compute each probability in log space before summing. Individual
    # coefficients can exceed float range at ordinary benchmark sizes.
    log_factorial_n = lgamma(n + 1)
    log_p, log_q = log(p), log1p(-p)
    return min(1.0, fsum(
        exp(log_factorial_n - lgamma(i + 1) - lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
        for i in range(k + 1)
    ))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float | None, float | None]:
    """Exact binomial interval by bisection on the binomial CDF."""
    if n == 0:
        return (None, None)
    if k == 0:
        low = 0.0
    else:
        a, b = 0.0, 1.0
        for _ in range(200):
            m = (a + b) / 2
            if 1 - binom_cdf(k - 1, n, m) > alpha / 2:
                b = m
            else:
                a = m
        low = (a + b) / 2
    if k == n:
        high = 1.0
    else:
        a, b = 0.0, 1.0
        for _ in range(200):
            m = (a + b) / 2
            if binom_cdf(k, n, m) < alpha / 2:
                b = m
            else:
                a = m
        high = (a + b) / 2
    return low, high


def merge_attempts(batches: list[tuple[str, list[RunRow]]]) -> list[RunRow]:
    """One row per instance, from the attempts made at it, in attempt order.

    ``batches`` is ``[(out-dir name, its rows), ...]``, oldest first. Six runs
    on 2026-09-04 ended ``failed`` with ``APIError: Upstream request failed``:
    the endpoint dropped them, not the model. Each still wrote a prediction
    row, so re-launching the original batch resumes nothing; the second attempt
    runs in an out-dir of its own and is folded back in here.

    The rule, and it is the only one that does not quietly change a
    denominator: an instance reports its **last attempt that ran** -- the last
    whose status is not in ``INVALID_STATUSES``. When no attempt ran, the last
    one is kept rather than dropped, because an instance that vanishes from the
    table is an instance nobody counts as lost. Earlier attempts are not summed
    into anything: what they spent was spent, but the cell is one observation
    per instance and a token total over two attempts at one instance is a
    number about the endpoint, not about the arm.
    """
    order: list[str] = []
    tries: dict[str, list[tuple[str, RunRow]]] = {}
    for name, rows in batches:
        for row in rows:
            if row.instance_id not in tries:
                tries[row.instance_id] = []
                order.append(row.instance_id)
            tries[row.instance_id].append((name, row))
    merged: list[RunRow] = []
    for instance_id in order:
        made = tries[instance_id]
        chosen = len(made) - 1
        for index in range(len(made) - 1, -1, -1):
            if made[index][1].valid:
                chosen = index
                break
        name, row = made[chosen]
        row.attempt = chosen + 1
        row.attempts = len(made)
        row.source_batch = name
        row.attempt_tokens_total = sum(attempt.tokens for _source, attempt in made)
        merged.append(row)
    return merged


def order_rows(rows: list[RunRow], order_csv: str | Path | None) -> tuple[list[RunRow], list[str]]:
    """Rows in the suite's frozen order, plus the instances the suite has but the cell does not."""
    if order_csv is None:
        return rows, []
    with Path(order_csv).open(encoding="utf-8", newline="") as handle:
        wanted = [r["instance_id"] for r in csv.DictReader(handle)]
    index = {r.instance_id: r for r in rows}
    ordered = [index[i] for i in wanted if i in index]
    extra = [r for r in rows if r.instance_id not in set(wanted)]
    return ordered + extra, [i for i in wanted if i not in index]


def summarize(
    rows: list[RunRow],
    expected_card: str | None = None,
    team: bool = True,
    alpha_readable: bool | None = None,
    timeout_s: float | None = None,
    withdrawn: Sequence[tuple[str, str, str, str]] = (),
) -> dict[str, Any]:
    """Counts over the cell.

    ``withdrawn`` is the withdrawal ledger, ``[(the instance that came back,
    the stand-in's instance, the stand-in's batch, why), ...]``. It is passed
    in rather than read off ``rows`` because a withdrawn replacement leaves no
    row here: its run is in the document's ``excluded``.

    ``team`` says whether the cell has seats to lay out; ``alpha_readable``
    says whether its delivery rate is alpha. They are different questions and
    used to be one flag. A single agent has nobody to deliver to, so alpha is
    undefined there, not zero -- and a scripted workflow *has* the seats but
    makes no choice, so its delivery rate measures its script. It defaults to
    ``team`` so every existing caller reads exactly as before.
    """
    valid = [r for r in rows if r.valid]
    if alpha_readable is None:
        alpha_readable = team
    delivered = sum(1 for r in valid if r.delivered) if alpha_readable else None
    low, high = clopper_pearson(delivered, len(valid)) if alpha_readable else (None, None)
    every = sum(1 for r in valid if r.every_delegate) if alpha_readable else None
    every_low, every_high = clopper_pearson(every, len(valid)) if alpha_readable else (None, None)
    cards = sorted({r.card for r in rows if r.card})
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    return {
        "runs": len(rows),
        "valid": len(valid),
        "invalid": [r.instance_id for r in rows if not r.valid],
        "team": team,
        "alpha_readable": alpha_readable,
        "delivered": delivered,
        "alpha": (delivered / len(valid)) if (alpha_readable and valid) else None,
        "ci95": [low, high] if alpha_readable else None,
        # Beside alpha and never instead of it: alpha is "a delegate seat was
        # used", this is "every declared delegate seat was used". They are the
        # same number on a roster with one delegate and diverge on any other,
        # and on the two-candidate roster it is this one that says whether the
        # organization the card describes actually happened.
        "every_delegate": every,
        "every_delegate_rate": (every / len(valid)) if (alpha_readable and valid) else None,
        "every_delegate_ci95": [every_low, every_high] if alpha_readable else None,
        # What the arm that scripts its topology reports instead: not a rate at
        # which an agent chose, a count of how much of a fixed topology carried
        # anything.
        "edges_walked": sum(r.edges_walked or 0 for r in rows if r.edges_declared),
        "edges_declared": sum(r.edges_declared or 0 for r in rows),
        "message_agent_attempts": sum(s.msg_agent for r in rows for s in r.seats.values()),
        "message_agent_sent": sum(s.msg_agent_sent for r in rows for s in r.seats.values()),
        # Two keys, not one: a cell where no run probed the tree and a cell
        # where every run probed it and found the analyst had written nothing
        # both give a count of zero, and they are different findings.
        "analyst_wrote_source": sum(1 for r in rows if r.analyst_wrote_source),
        "analyst_wrote_source_probed": sum(1 for r in rows if r.analyst_wrote_source is not None),
        "edges_walked_rate": (
            sum(r.edges_walked or 0 for r in rows if r.edges_declared) / sum(r.edges_declared or 0 for r in rows)
            if sum(r.edges_declared or 0 for r in rows)
            else None
        ),
        # Averaging hides the shape: one run walking none of its six and six
        # runs each missing one are the same rate.
        # Whether an edge set was declared at all. ``edges_declared == 0`` is
        # what an arm that declares none and an arm whose declaration was never
        # read both produce, and those are different findings.
        "edges_declared_state": ("declared" if any(r.edges_declared is not None for r in rows) else "not_declared"),
        "edges_unwalked": [
            [r.instance_id, r.edges_walked or 0, r.edges_declared]
            for r in rows
            if r.edges_declared and (r.edges_walked or 0) < r.edges_declared
        ],
        "statuses": statuses,
        # The retry ledger. Present on every cell, including the ones nothing
        # was retried in, so "no instance needed a second attempt" and "this
        # report does not say" are not the same blank.
        "retried": [r.instance_id for r in rows if r.attempts > 1],
        "retried_count": sum(1 for r in rows if r.attempts > 1),
        # A second attempt that ran where the first did not.
        "retry_succeeded": [r.instance_id for r in rows if r.attempts > 1 and r.attempt > 1 and r.valid],
        "retry_succeeded_count": sum(1 for r in rows if r.attempts > 1 and r.attempt > 1 and r.valid),
        # No attempt at this instance ever reached the model. Identical to
        # ``invalid`` on a cell with no retries, and deliberately so: it is the
        # same fact, counted after every attempt has been made.
        "infra_failed": [r.instance_id for r in rows if not r.valid],
        "infra_failed_count": sum(1 for r in rows if not r.valid),
        "attempt_sources": sorted({r.source_batch for r in rows if r.source_batch}),
        # The replacement ledger, and the reason it is a list of the instances
        # that *left*: those are the ones no number here counts any more. Their
        # run rows are in the document's ``excluded``, not deleted -- a paid run
        # that no denominator holds still has to be findable.
        "replaced": [r.replacement_for for r in rows if r.replacement_for],
        "replaced_count": sum(1 for r in rows if r.replacement_for),
        "replaced_by": [[r.replacement_for, r.instance_id, r.source_batch] for r in rows if r.replacement_for],
        # The mirror of the ledger above, and the reason both are written: a
        # withdrawn replacement is not a replacement that never happened. The
        # stand-in was run and paid for, and its row is in ``excluded``. These
        # keys are what a later reader has to find the episode by -- and what
        # tells a cell that never had a replacement apart from one whose
        # replacement was taken back.
        "replacements_withdrawn": [gone for gone, _stands_in, _source, _why in withdrawn],
        "replacements_withdrawn_count": len(withdrawn),
        "replacements_withdrawn_by": [list(entry) for entry in withdrawn],
        "cap_hit": [r.instance_id for r in rows if r.cap_hit],
        # The two halves of ``cap_hit``, kept apart because only the second is
        # a run whose outcome the budget chose.
        "cap_hit_precheck": [r.instance_id for r in rows if r.cap_hit_precheck],
        "cap_hit_postcall": [r.instance_id for r in rows if r.cap_hit_postcall],
        "cap_hit_aggregate": [r.instance_id for r in rows if r.cap_hit_aggregate],
        # Recorded: the allowances this cell's sessions were actually handed.
        # More than one value is not a fault -- a session resuming a partly
        # spent seat is handed the remainder -- but a cell whose largest value
        # is not the budget the spec declared is a cell that did not run at the
        # budget it says it ran at.
        "seat_cap_tokens": sorted({r.seat_cap for r in rows if r.seat_cap is not None}),
        "seat_cap_unknown": [r.instance_id for r in rows if r.seat_cap is None],
        # The workflow's own verdict, not this reader's.
        "seat_budget_exhausted": [r.instance_id for r in rows if r.budget_exhausted],
        # Derived. The tightest runs first, so a run that finished on a seat it
        # had all but spent is visible without re-deriving the subtraction.
        "seat_headroom_min": sorted(
            ([r.instance_id, r.seat_headroom_min] for r in rows if r.seat_headroom_min is not None),
            key=lambda pair: pair[1],
        ),
        # Not a quantity about the runs: a quantity about the reading of them.
        # Every count below that comes off a seat has this as its denominator,
        # so a cell where it is short of ``runs`` has counts that are floors.
        "seat_snapshot_found": sum(1 for r in rows if r.seat_snapshot_found),
        "seat_snapshot_missing": [r.instance_id for r in rows if not r.seat_snapshot_found],
        "seat_snapshot_missing_count": sum(1 for r in rows if not r.seat_snapshot_found),
        # The runs where summing the seat files and the workflow's own
        # ``seat_spend`` ledger do not agree. Empty is the expected reading;
        # a non-empty list means one of the two is wrong and neither column
        # can be quoted until it is settled.
        "seat_spend_disagrees": [r.instance_id for r in rows if r.seat_spend_agrees is False],
        # Censoring, on every arm, by the rule the single arm's driver uses.
        "timeout_s": timeout_s,
        "timeout_censored": [r.instance_id for r in rows if r.timeout_censored],
        "timeout_censored_count": sum(1 for r in rows if r.timeout_censored),
        # The second, independent reading: the clock against the wall the spec
        # set. It is derived and it is here to contradict the first one -- a
        # run past the wall without the reason, or the reason without the run
        # time, means one of the two is not measuring what it is read as.
        "duration_at_or_over_timeout": [
            r.instance_id
            for r in rows
            if timeout_s is not None and r.duration_s is not None and r.duration_s >= timeout_s
        ],
        "timeout_rule_disagreement": (
            [
                r.instance_id
                for r in rows
                if r.duration_s is not None and r.timeout_censored != (r.duration_s >= timeout_s)
            ]
            if timeout_s is not None
            else []
        ),
        # And against the single arm's own flag, where the record has one.
        "timeout_flag_disagreement": [
            r.instance_id for r in rows if r.timeout_recorded is not None and r.timeout_recorded != r.timeout_censored
        ],
        "duration_max_s": max((r.duration_s for r in rows if r.duration_s is not None), default=None),
        "analyst_cards": cards,
        "card_matches_expected": (cards == [expected_card]) if expected_card else None,
        "team_configs": sorted({r.team_config for r in rows if r.team_config}),
        "tokens_total": sum(r.tokens for r in rows),
        "tokens_all_attempts": sum(
            r.attempt_tokens_total if r.attempt_tokens_total is not None else r.tokens for r in rows
        ),
    }
