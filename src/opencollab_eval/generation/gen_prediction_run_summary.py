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

from dataclasses import fields
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


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


_METRICS_FLAT_DUMP_SKIPS = frozenset({"patch", "tree_snapshots"})


def workflow_result_metrics(result) -> dict:
    metrics = {
        field.name: _json_safe(getattr(result, field.name))
        for field in fields(result)
        if field.name not in _METRICS_FLAT_DUMP_SKIPS
    }
    # The graded tree at each seat boundary, for the arms that record them.
    # A workflow arm carries its own equivalent inside ``workflow_result``
    # (``self_collaboration`` writes ``tree_snapshots`` there), so a reader that
    # wants either takes this key first and falls back to that one.
    if result.tree_snapshots is not None:
        metrics["tree_snapshots"] = _json_safe(result.tree_snapshots)
    # The same quantities again, under the names the single-agent path also
    # writes them under. Without this the two arms' records can only be read
    # one arm at a time; see ``gen_prediction_run_summary``.
    status = result.runtime_status
    reason = result.runtime_reason
    workflow_result = getattr(result, "workflow_result", None)
    if isinstance(workflow_result, dict) and workflow_result.get("status") == "error":
        # A workflow that returns an error has stopped, but the runtime around
        # it returned normally, so it reports "completed" and no reason at all.
        # Left alone the very same stop reads as "completed" on the workflow
        # arms and as "stopped" on the single-agent arm: an arm-varying
        # recording rule on the field a cross-arm reader selects. Record the
        # stop, and carry the workflow's own error string as the detail so the
        # reason is non-empty here exactly as it is on the single-agent arm.
        status = "stopped"
        reason = reason or str(workflow_result.get("error") or "workflow error")
    metrics[RUN_SUMMARY_KEY] = build_run_summary(
        steps=result.steps,
        tokens=result.tokens_used,
        status=status,
        reason=reason,
        duration_s=result.duration,
        error=result.error,
    )
    return metrics
