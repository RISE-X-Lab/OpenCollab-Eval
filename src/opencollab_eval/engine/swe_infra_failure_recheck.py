"""Recompute arm-level SWE-bench infra-failure flags on top of the official harness.

`swebench.harness.reporting.make_run_report` -- part of the third-party `swebench`
PyPI package (not this repository) -- writes an arm-level report whose
`failure_reasons` / `infra_failure_ids` / `ambiguous_failure_ids` fields come from
`swebench.harness.infra_failure.classify_logs`: a full-text regex scan over the
concatenated `test_output.txt` + `run_instance.log` of *every* unresolved or
errored instance (swebench/harness/reporting.py:100-107 in swebench 5.0.2). That
scan runs unconditionally, with no check for whether the per-instance grader
(`swebench.harness.grading.get_eval_report`) already parsed real FAIL_TO_PASS
results for the instance.

In practice this misfires whenever a sandboxed evaluation container has no PyPI
access: an early, structurally-doomed `pip install -e .[dev]` step in `eval.sh`
prints `Temporary failure in name resolution` on every run of the affected repos,
regardless of outcome. The arm-level scan matches that text and reports
`network_unreachable`, silently outranking real FAIL_TO_PASS failures that were
correctly parsed later in the very same log -- i.e. a "the model did not fix the
bug" result gets mislabeled as an infrastructure fault.

This module does not patch the `swebench` package (it is official, third-party
code -- see the module docstring above for why). It instead recomputes the
verdict on our side using the one signal the arm-level scan ignores: whether the
per-instance `report.json` shows real, parsed FAIL_TO_PASS results. Where it
does, the arm-level flag is a false positive and is dropped. Where the instance
never got that far (no parsed test results -- e.g. the container's own patch
caused a collection-time crash before any test ran), this module leaves the
original classification untouched: there is no stronger evidence available here
to contradict it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_fail_to_pass_fraction import fail_to_pass_outcome

__all__ = [
    "instance_has_parsed_grading",
    "load_instance_reports_for_arm",
    "recompute_arm_failure_reasons",
]


def instance_has_parsed_grading(instance_report: dict[str, Any] | None) -> bool:
    """Return True if the harness parsed real FAIL_TO_PASS results for this instance.

    A per-instance report.json only reaches this state when
    `swebench.harness.grading.get_logs_eval` succeeded at parsing structured
    PASSED/FAILED markers from the container's test output (`patch_successfully_applied`
    is True) and at least one FAIL_TO_PASS test id was actually graded. When that
    holds, any keyword the arm-level scan finds elsewhere in the same raw log is
    noise, not evidence of an environment fault that prevented evaluation.
    """
    outcome = fail_to_pass_outcome(instance_report)
    return outcome.patch_successfully_applied and outcome.graded


def recompute_arm_failure_reasons(
    arm_report: dict[str, Any],
    instance_reports: dict[str, dict[str, Any] | None],
) -> dict[str, Any]:
    """Return a corrected copy of an arm-level SWE-bench report.

    `instance_reports` maps instance_id -> that instance's own report.json payload
    (the value under `report[instance_id]`, not the whole file), or None/missing if
    it could not be loaded. Every instance_id currently in `arm_report["failure_reasons"]`
    whose own report shows real parsed grading (see `instance_has_parsed_grading`) is
    removed from `failure_reasons`, `infra_failure_ids`, and `ambiguous_failure_ids`,
    and recorded in the returned `corrected_false_infra_flags` audit trail. Everything
    else in `arm_report` is passed through unchanged.
    """
    failure_reasons = dict(arm_report.get("failure_reasons") or {})
    infra_failure_ids = set(arm_report.get("infra_failure_ids") or [])
    ambiguous_failure_ids = set(arm_report.get("ambiguous_failure_ids") or [])

    corrected_false_infra_flags: dict[str, Any] = {}
    for instance_id in list(failure_reasons):
        instance_report = instance_reports.get(instance_id)
        if not instance_has_parsed_grading(instance_report):
            continue
        reason = failure_reasons.pop(instance_id)
        was_infra_tier = instance_id in infra_failure_ids
        infra_failure_ids.discard(instance_id)
        ambiguous_failure_ids.discard(instance_id)
        outcome = fail_to_pass_outcome(instance_report)
        corrected_false_infra_flags[instance_id] = {
            "original_reason": reason,
            "original_tier": "environment" if was_infra_tier else "ambiguous",
            "f2p_failure_count": outcome.f2p_failure_count,
            "f2p_success_count": outcome.f2p_success_count,
        }

    result = dict(arm_report)
    result["failure_reasons"] = failure_reasons
    result["infra_failure_ids"] = sorted(infra_failure_ids)
    result["ambiguous_failure_ids"] = sorted(ambiguous_failure_ids)
    result["infra_failure_instances"] = len(infra_failure_ids)
    result["ambiguous_failure_instances"] = len(ambiguous_failure_ids)
    result["corrected_false_infra_flags"] = corrected_false_infra_flags
    return result


def load_instance_reports_for_arm(
    logs_root: Path,
    run_id: str,
    model_name: str,
    instance_ids: list[str],
) -> dict[str, dict[str, Any] | None]:
    """Load each instance's own report.json payload from the harness log tree.

    Mirrors the path the `swebench` harness itself writes to:
    `<logs_root>/<run_id>/<model_name with "/" -> "__">/<instance_id>/report.json`.
    An instance whose file is missing or unparseable maps to None rather than
    raising, so a caller can still recompute the rest of the report.
    """
    safe_model = model_name.replace("/", "__")
    reports: dict[str, dict[str, Any] | None] = {}
    for instance_id in instance_ids:
        report_path = Path(logs_root) / run_id / safe_model / instance_id / "report.json"
        try:
            content = report_path.read_text(encoding="utf-8")
        except OSError:
            reports[instance_id] = None
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            reports[instance_id] = None
            continue
        value = payload.get(instance_id) if isinstance(payload, dict) else None
        reports[instance_id] = value if isinstance(value, dict) else None
    return reports
