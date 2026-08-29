"""One block of run-level fields that every arm writes under the same names.

The two generators build their metric records from different objects. The
single-agent path reads a ``RunResult`` (``tokens``, ``status``, ``reason``);
the workflow/team path dumps the fields of an ``EvalResult`` (``tokens_used``,
``steps``, ``runtime_status``, ``runtime_reason``, ``duration``). Neither
record is wrong on its own. The failure appears only when they are read
together: a script that selects ``used_tokens`` returns every single-agent row
and silently drops every team row. No error, just a shorter table -- and a
shorter table is what an arm with fewer completed runs also looks like.

So both paths additionally write ``run_summary``, whose key set is fixed here
and pinned equal across the two arms by the tests. Read a cross-arm quantity
from this block. The arm-native keys stay exactly where they are, because the
readers already selecting them are correct within one arm.

``steps`` and ``tokens`` are run totals. On a team that is the sum over the
agents that ran -- the quantity the shared budget pool is held equal on -- so
it is a per-run number, not a per-agent one; the per-agent split is in the
trajectory. ``status`` is the run's terminal disposition as the runtime
reported it ("completed" / "stopped" / "failed"), and ``reason`` is that
runtime's own detail string, which is why a budget stop and a step-ceiling
stop are distinguishable here and not in ``status`` alone.
"""

from __future__ import annotations

from typing import Any

RUN_SUMMARY_KEY = "run_summary"

RUN_SUMMARY_FIELDS: tuple[str, ...] = (
    "steps",
    "tokens",
    "status",
    "reason",
    "duration_s",
    "error",
)


def build_run_summary(
    *,
    steps: Any,
    tokens: Any,
    status: Any,
    reason: Any,
    duration_s: Any,
    error: Any,
) -> dict[str, Any]:
    """Build the cross-arm block. Every field is present on every arm.

    A quantity the run did not produce is written as ``None`` rather than
    omitted, so a missing key always means the writer is out of date and never
    means the run had nothing to report.
    """
    return {
        "steps": None if steps is None else int(steps),
        "tokens": None if tokens is None else int(tokens),
        "status": None if status is None else str(status),
        "reason": None if reason is None else str(reason),
        "duration_s": None if duration_s is None else float(duration_s),
        "error": None if error is None else str(error),
    }


__all__ = ["RUN_SUMMARY_FIELDS", "RUN_SUMMARY_KEY", "build_run_summary"]
