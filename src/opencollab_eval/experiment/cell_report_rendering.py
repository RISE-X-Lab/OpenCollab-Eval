"""Text and JSON rendering for cell reports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from opencollab_eval.experiment.cell_report_rows import RunRow


def _timeout_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The censoring count, worded and derived identically on every arm.

    Printed unconditionally, including when it is zero: "no run was cut off"
    and "this report does not say" were the same blank line on two of the three
    arms, and they are the two whose wall-clock window is the shorter one.
    """
    wall = summary.get("timeout_s")
    lines.append(
        f"cut off at the wall clock: {summary['timeout_censored_count']}/{summary['runs']}"
        + (f" (spec timeout {wall:g}s)" if isinstance(wall, int | float) else " (no spec timeout given)")
        + f" -> {summary['timeout_censored']}"
    )
    disagree = summary.get("timeout_rule_disagreement") or []
    flag_disagree = summary.get("timeout_flag_disagreement") or []
    if disagree or flag_disagree:
        lines.append(
            "  !! timeout readings disagree"
            + (f" -- run time vs recorded reason: {disagree}" if disagree else "")
            + (f" -- recorded flag vs reason: {flag_disagree}" if flag_disagree else "")
        )


def _replacement_note(row: RunRow) -> str:
    """Which pre-registered instance this row stands in for, where it stands in for one."""
    return f"   [replaces {row.replacement_for}]" if row.replacement_for else ""


def _attempt_note(row: RunRow) -> str:
    """Which attempt this row is, printed only where there was more than one."""
    if row.attempts <= 1:
        return ""
    return f"   [attempt {row.attempt} of {row.attempts} from {row.source_batch}]"


def _retry_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The retry ledger, printed only on a cell that has one."""
    if not summary.get("retried"):
        return
    lines.append(
        f"retried instances: {summary['retried_count']} -> {summary['retried']}"
        f"   (merged from {summary['attempt_sources']})"
    )
    lines.append(f"  a later attempt ran: {summary['retry_succeeded_count']} -> {summary['retry_succeeded']}")
    lines.append(f"  no attempt ever ran: {summary['infra_failed_count']} -> {summary['infra_failed']}")


def _edges_short(summary: dict[str, Any], lines: list[str]) -> None:
    """The runs that fell short of their own declared edges, named not averaged.

    One run walking none of its six and six runs each missing one are the same
    rate.
    """
    lines.append(
        f"  runs short of their declared edges: {len(summary['edges_unwalked'])}"
        f" -> {[[i, f'{w}/{d}'] for i, w, d in summary['edges_unwalked']]}"
    )


def _edge_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """What the declared topology carried, on an arm that also reports alpha."""
    if summary.get("edges_declared_state") != "declared":
        lines.append(
            "edges declared: none (this arm's runs record no assigned topology,"
            " which is not the same as declaring some and walking none)"
        )
        return
    rate = summary["edges_walked_rate"]
    lines.append(
        f"edges walked {summary['edges_walked']}/{summary['edges_declared']}"
        + (f" = {rate:.3f}" if rate is not None else "")
        + "   (a declared channel that carried at least one message_agent call;"
        " delegation is the line above, and the two are different questions)"
    )
    _edges_short(summary, lines)


def _replacement_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The replacement ledger, printed only on a cell that has one."""
    if not summary.get("replaced_by"):
        return
    lines.append(f"replaced instances (evaluation environment unusable): {summary['replaced_count']}")
    for gone, stands_in, source in summary["replaced_by"]:
        lines.append(f"  {gone} -> {stands_in}   (next row of the ordered draw, from {source})")
    lines.append("  the replaced runs are in the JSON under 'excluded'; they enter no denominator here")


def _withdrawal_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The withdrawal ledger, printed only on a cell whose replacement was taken back."""
    if not summary.get("replacements_withdrawn_by"):
        return
    lines.append(f"withdrawn replacements: {summary['replacements_withdrawn_count']}")
    for gone, stands_in, source, why in summary["replacements_withdrawn_by"]:
        lines.append(f"  {stands_in} (from {source}) no longer stands in for {gone}: {why}")
    lines.append("  the stand-in runs are in the JSON under 'excluded'; they enter no denominator here")


def _headroom(row: RunRow) -> str:
    """The tightest seat's remaining allowance, or ``?`` when none was recorded.

    ``?`` and ``0`` are different findings and the column has to keep them
    apart: the first says the ceiling was not in the files, the second says the
    seat was spent to the last token.
    """
    return "?" if row.seat_headroom_min is None else f"{row.seat_headroom_min:,}"


def _cap_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The three cap counts, worded and split identically on every arm."""
    lines.append(f"seat at budget cap: {len(summary['cap_hit'])}/{summary['runs']} -> {summary['cap_hit']}")
    lines.append(
        f"  stopped by the pre-call reservation: {len(summary['cap_hit_precheck'])} -> {summary['cap_hit_precheck']}"
    )
    lines.append(
        f"  overspent after a call returned:     {len(summary['cap_hit_postcall'])} -> {summary['cap_hit_postcall']}"
    )
    lines.append(
        f"  stopped at the aggregate ceiling:    {len(summary['cap_hit_aggregate'])} -> {summary['cap_hit_aggregate']}"
    )
    lines.append(
        "seat allowance handed to a session (recorded, session_terminal.max_budget_tokens): "
        + (str(summary["seat_cap_tokens"]) or "[]")
        + (f"   unknown on {len(summary['seat_cap_unknown'])}/{summary['runs']}" if summary["seat_cap_unknown"] else "")
    )
    lines.append(
        f"seat budget exhausted (the workflow's own verdict): {len(summary['seat_budget_exhausted'])}"
        f" -> {summary['seat_budget_exhausted']}"
    )
    # Derived, and said so: no record anywhere says what a seat had left when
    # it stopped. A seat that answered with 0.25% of its allowance unspent
    # reads in every other column exactly like one that answered with 90%
    # unspent, and that difference is what the cap columns cannot show.
    tightest = summary["seat_headroom_min"][:3]
    lines.append(
        "smallest seat headroom left (derived = allowance - the role's summed spend), tightest first: "
        + (", ".join(f"{instance} {value:,}" for instance, value in tightest) or "n/a (no allowance recorded)")
    )


def _render_single(rows: list[RunRow], summary: dict[str, Any], lines: list[str]) -> str:
    lines.append(
        f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'steps':>5s} {'patch':>6s} "
        f"{'left':>9s} {'cut':>3s} cap"
    )
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {r.steps:>5} {str(r.patch_chars or 0):>6} "
            f"{_headroom(r):>9s} {'CUT' if r.timeout_censored else '-':>3s} "
            + (",".join(r.cap_hit) or "-")
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
            + _attempt_note(r)
            + _replacement_note(r)
        )
    lines.append("")
    lines.append(
        f"valid {summary['valid']}/{summary['runs']}"
        + (f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else "")
        + "   (delivery is a team-arm quantity; none is computed here)"
    )
    lines.append(f"statuses: {summary['statuses']}")
    _edge_lines(summary, lines)
    _retry_lines(summary, lines)
    _replacement_lines(summary, lines)
    _withdrawal_lines(summary, lines)
    _cap_lines(summary, lines)
    _timeout_lines(summary, lines)
    _token_lines(summary, lines)
    return "\n".join(lines)


def render(rows: list[RunRow], summary: dict[str, Any], missing: list[str]) -> str:
    lines = []
    if missing:
        lines.append(f"MISSING FROM METRICS ({len(missing)}): {missing}")
    # Printed above everything else, because it is a statement about whether
    # the rest of the report can be read at face value: on these runs the seat
    # columns and all three cap counts are zero for want of a file, which is
    # spelt exactly like a run that never hit its cap.
    if summary.get("seat_snapshot_missing"):
        lines.append(
            "!! SEAT SNAPSHOT NOT FOUND for"
            f" {summary['seat_snapshot_missing_count']}/{summary['runs']} runs"
            " -- their seat, delivery and cap columns are zero for want of a file,"
            f" not for want of a stop: {summary['seat_snapshot_missing']}"
        )
    if not summary.get("team", True):
        return _render_single(rows, summary, lines)
    header = (
        f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'analyst':>9s} {'coder':>8s} {'tester':>8s} "
        f"{'left':>9s} {'cut':>3s} {'deleg':5s} {'msgA':>4s} {'aWr':>3s} {'snap':>4s} cap"
    )
    lines.append(header)
    for i, r in enumerate(rows, 1):
        # Summed over the seats that held the role, never the last seat that
        # held it: the workflow arm seats its analyst twice.
        a_tok, c_tok, t_tok = (r.role_tokens.get(k, 0) for k in ("analyst", "coder", "tester"))
        a_writes = sum(s.writes for s in r.seats.values() if s.role == "analyst")
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {a_tok:>9,} {c_tok:>8,} {t_tok:>8,} "
            f"{_headroom(r):>9s} {'CUT' if r.timeout_censored else '-':>3s} "
            f"{'YES' if r.delivered else 'no':5s} {sum(s.msg_agent for s in r.seats.values()):>4} {a_writes:>3} "
            f"{r.tree_snapshots:>4} {','.join(r.cap_hit) or '-'}"
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
            + _attempt_note(r)
            + _replacement_note(r)
        )
    lines.append("")
    excluded = f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else ""
    if summary.get("alpha_readable", summary.get("team", True)):
        lo, hi = summary["ci95"]
        alpha = summary["alpha"]
        lines.append(
            f"delivered {summary['delivered']}/{summary['valid']} valid"
            + (f" = {alpha:.3f}   Clopper-Pearson 95% [{lo:.3f}, {hi:.3f}]" if alpha is not None else "")
            + excluded
        )
        # The second rate, printed whenever it can differ from the first. On a
        # roster with one delegate the two are the same number and saying it
        # twice reads as two findings; on any other roster the gap between them
        # is a fact about what the run did, and it is the quantity a
        # two-candidate cell is actually about.
        every = summary.get("every_delegate")
        rate = summary.get("every_delegate_rate")
        if every is not None and every != summary["delivered"]:
            lo, hi = summary["every_delegate_ci95"]
            lines.append(
                f"every delegate seat {every}/{summary['valid']} valid"
                + (f" = {rate:.3f}   Clopper-Pearson 95% [{lo:.3f}, {hi:.3f}]" if rate is not None else "")
            )
        # Beside alpha, never instead of it. The team file declares which role
        # may address which; alpha says whether an agent chose to hand the work
        # on. A cell can walk an edge and deliver nothing, and reading either
        # number off the other is how the two get confused.
        _edge_lines(summary, lines)
    else:
        # No delivery rate on this arm and no interval: its edges are written
        # by a script, so what the runs vary is whether each fixed edge carried
        # anything. That is what gets reported, with the runs that fell short
        # named rather than averaged away.
        rate = summary["edges_walked_rate"]
        lines.append(
            f"edges walked {summary['edges_walked']}/{summary['edges_declared']}"
            + (f" = {rate:.3f}" if rate is not None else "")
            + "   (the topology is script-fixed on this arm, so there is no"
            " delegation rate to read)" + excluded
        )
        _edges_short(summary, lines)
    lines.append(f"statuses: {summary['statuses']}")
    _retry_lines(summary, lines)
    _replacement_lines(summary, lines)
    _withdrawal_lines(summary, lines)
    _cap_lines(summary, lines)
    _timeout_lines(summary, lines)
    lines.append(
        f"analyst card digests: {summary['analyst_cards']}"
        + (
            ""
            if summary["card_matches_expected"] is None
            else ("  == expected" if summary["card_matches_expected"] else "  != EXPECTED")
        )
    )
    lines.append(f"team configs: {summary['team_configs']}")
    _token_lines(summary, lines)
    return "\n".join(lines)


def _run_document(row: RunRow) -> dict[str, Any]:
    return {
        **{k: v for k, v in asdict(row).items() if k != "seats"},
        "seats": {k: asdict(s) for k, s in row.seats.items()},
    }


def _token_lines(summary: dict[str, Any], lines: list[str]) -> None:
    lines.append(f"tokens total (selected attempts): {summary['tokens_total']:,}")
    lines.append(f"tokens across recorded attempts: {summary.get('tokens_all_attempts', summary['tokens_total']):,}")


def report_document(
    rows: list[RunRow],
    summary: dict[str, Any],
    missing: list[str],
    excluded: Sequence[tuple[RunRow, str]] = (),
) -> dict[str, Any]:
    """The report as JSON: the cell's rows, and the paid rows no number counts.

    ``excluded`` is ``[(row, why), ...]``. It holds the runs of an instance the
    cell no longer reports -- today only a replaced one. They are kept in the
    document rather than dropped because they were paid for and because the
    next reader has to be able to see what was spent on an instance that ended
    up in no denominator.
    """
    return {
        "summary": summary,
        "recorded_tokens_including_excluded": sum(
            r.attempt_tokens_total if r.attempt_tokens_total is not None else r.tokens
            for r in [*rows, *(row for row, _reason in excluded)]
        ),
        "missing": missing,
        "runs": [_run_document(r) for r in rows],
        "excluded": [{**_run_document(r), "excluded_reason": why} for r, why in excluded],
    }
