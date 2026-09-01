"""Compute the graded outcome ``y`` in [0, 1] from an official SWE-bench report.

The experiment's outcome variable is not the boolean the harness prints in its
arm-level summary. It is the *fraction* of an instance's fail-to-pass tests that
pass, with skipped tests counted as unresolved. The official per-instance
``report.json`` already carries both halves of that fraction --
``tests_status.FAIL_TO_PASS.success`` and ``.failure`` -- because
``swebench.harness.run_evaluation`` calls ``get_eval_report`` with
``include_tests_status=True`` unconditionally. Nothing new has to be run to
recover ``y``; it only has to be read and stored.

Two deliberate departures from the official helpers, both of which change a
number the paper reports:

1. **An empty denominator is 0, not 1.** ``swebench.harness.grading`` returns
   ``1`` from ``compute_fail_to_pass`` when no fail-to-pass test was graded at
   all. An empty denominator means the patch never applied, or the log never
   parsed -- the run produced no evidence about the bug. Scoring that as a
   perfect outcome is the silent-wrong-answer failure mode: it moves an arm's
   mean up in exactly the cases where the arm produced nothing. Here it is 0,
   and ``graded`` is False so the two zeros stay distinguishable.

2. **``y == 1`` is not ``resolved``.** The official ``resolved`` flag also
   requires every pass-to-pass test to hold (``get_resolution_status``). Both
   are kept, side by side, so neither can be mistaken for the other.

The raw counts and the denominator are stored alongside the ratio. The ratio is
recomputable from the counts and the counts are not recomputable from the ratio,
and the denominator is itself the diagnostic: ``0`` means something entirely
different from ``3``.

The skipped-counted-as-unresolved rule is enforced by the harness version, not
here: ``swebench`` 5.x puts a SKIPPED fail-to-pass test in the ``failure`` list,
while 4.x puts it in neither and silently shrinks the denominator. See the
``swebench`` pin in ``pyproject.toml``. For reports whose provenance is unknown,
``gold_denominator_check`` compares the denominator against the instance's gold
fail-to-pass list and reports any test that went missing from both lists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FailToPassOutcome",
    "UNGRADED_NO_FAIL_TO_PASS_TESTS",
    "UNGRADED_NO_PATCH",
    "UNGRADED_NO_REPORT",
    "UNGRADED_PATCH_NOT_APPLIED",
    "fail_to_pass_outcome",
    "gold_denominator_check",
    "load_instance_report",
    "outcome_from_report_file",
]

UNGRADED_NO_REPORT = "no_report"
UNGRADED_NO_PATCH = "no_patch"
UNGRADED_PATCH_NOT_APPLIED = "patch_not_applied_or_log_unparsed"
UNGRADED_NO_FAIL_TO_PASS_TESTS = "no_fail_to_pass_tests_recorded"

MAX_REPORT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FailToPassOutcome:
    """One run's graded outcome, with the counts the ratio was computed from."""

    y: float
    f2p_success_count: int
    f2p_failure_count: int
    f2p_denominator: int
    graded: bool
    ungraded_reason: str
    resolved: bool
    patch_successfully_applied: bool
    p2p_success_count: int
    p2p_failure_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "y": self.y,
            "f2p_success_count": self.f2p_success_count,
            "f2p_failure_count": self.f2p_failure_count,
            "f2p_denominator": self.f2p_denominator,
            "f2p_graded": self.graded,
            "ungraded_reason": self.ungraded_reason,
            "resolved": self.resolved,
            "patch_successfully_applied": self.patch_successfully_applied,
            "p2p_success_count": self.p2p_success_count,
            "p2p_failure_count": self.p2p_failure_count,
        }


def _test_ids(tests_status: object, section: str, bucket: str) -> list[str]:
    if not isinstance(tests_status, dict):
        return []
    part = tests_status.get(section)
    if not isinstance(part, dict):
        return []
    values = part.get(bucket)
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def fail_to_pass_outcome(instance_report: dict[str, Any] | None) -> FailToPassOutcome:
    """Read ``y`` and its counts out of one instance's ``report.json`` payload.

    ``instance_report`` is the value stored under the instance id inside the
    file, not the whole file. A missing or malformed payload yields ``y = 0``
    with ``graded`` False, which is the same answer an empty denominator gets:
    no evidence about the bug was produced.
    """
    if not isinstance(instance_report, dict):
        return FailToPassOutcome(
            y=0.0,
            f2p_success_count=0,
            f2p_failure_count=0,
            f2p_denominator=0,
            graded=False,
            ungraded_reason=UNGRADED_NO_REPORT,
            resolved=False,
            patch_successfully_applied=False,
            p2p_success_count=0,
            p2p_failure_count=0,
        )

    tests_status = instance_report.get("tests_status")
    success = _test_ids(tests_status, "FAIL_TO_PASS", "success")
    failure = _test_ids(tests_status, "FAIL_TO_PASS", "failure")
    denominator = len(success) + len(failure)
    applied = instance_report.get("patch_successfully_applied") is True

    if denominator > 0:
        # The one place the ratio is formed. Skipped fail-to-pass tests are
        # already inside `failure` when the report came from swebench 5.x.
        y = len(success) / denominator
        reason = ""
    else:
        # Deliberately 0, where the official compute_fail_to_pass returns 1.
        y = 0.0
        if instance_report.get("patch_is_None") is True or instance_report.get("patch_exists") is False:
            reason = UNGRADED_NO_PATCH
        elif not applied:
            reason = UNGRADED_PATCH_NOT_APPLIED
        else:
            reason = UNGRADED_NO_FAIL_TO_PASS_TESTS

    return FailToPassOutcome(
        y=y,
        f2p_success_count=len(success),
        f2p_failure_count=len(failure),
        f2p_denominator=denominator,
        graded=denominator > 0,
        ungraded_reason=reason,
        # Straight from the harness: resolved additionally requires every
        # pass-to-pass test to hold, so it is not implied by y == 1.
        resolved=instance_report.get("resolved") is True,
        patch_successfully_applied=applied,
        p2p_success_count=len(_test_ids(tests_status, "PASS_TO_PASS", "success")),
        p2p_failure_count=len(_test_ids(tests_status, "PASS_TO_PASS", "failure")),
    )


def load_instance_report(report_path: Path, instance_id: str = "") -> dict[str, Any] | None:
    """Return the per-instance payload inside a harness ``report.json``.

    The harness writes ``{instance_id: {...}}``. When ``instance_id`` is empty
    the single entry is taken, which is what the per-instance run always writes;
    a file with several entries requires the caller to name one. Missing,
    oversized, or unparseable files return None rather than raising, so a
    back-fill over an old batch reports the gap instead of stopping at it.
    """
    try:
        if report_path.stat().st_size > MAX_REPORT_BYTES:
            return None
        content = report_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    if instance_id:
        value = payload.get(instance_id)
    elif len(payload) == 1:
        value = next(iter(payload.values()))
    else:
        return None
    return value if isinstance(value, dict) else None


def outcome_from_report_file(report_path: Path, instance_id: str = "") -> FailToPassOutcome:
    """Compute ``y`` directly from a ``report.json`` on disk."""
    return fail_to_pass_outcome(load_instance_report(report_path, instance_id))


def gold_denominator_check(
    outcome: FailToPassOutcome,
    gold_fail_to_pass: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    """Compare a graded denominator against the instance's gold fail-to-pass list.

    Under the paper's rule every gold fail-to-pass test lands in ``success`` or
    ``failure``, so the denominator equals the gold count. A denominator that is
    short means tests fell out of both lists -- which is exactly what
    ``swebench`` 4.x does to a SKIPPED test -- and the ratio was then taken over
    a silently smaller population. Returns ``matches`` None when there is no
    gold list to check against, so "not checked" never reads as "checked and
    fine".
    """
    if not gold_fail_to_pass:
        return {"gold_count": None, "matches": None, "missing_from_denominator": None}
    gold_count = len(gold_fail_to_pass)
    return {
        "gold_count": gold_count,
        "matches": outcome.f2p_denominator == gold_count,
        "missing_from_denominator": gold_count - outcome.f2p_denominator,
    }
