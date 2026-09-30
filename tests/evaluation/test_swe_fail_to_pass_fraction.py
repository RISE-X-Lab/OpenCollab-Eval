from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.engine.swe_fail_to_pass_fraction import (
    UNGRADED_NO_FAIL_TO_PASS_TESTS,
    UNGRADED_NO_PATCH,
    UNGRADED_NO_REPORT,
    UNGRADED_PATCH_NOT_APPLIED,
    fail_to_pass_outcome,
    gold_denominator_check,
    load_instance_report,
    outcome_from_report_file,
)

# Shapes below follow real gpu3 tri15 report.json files, trimmed to the fields
# these functions read.

_PARTIAL = {
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": True,
    "resolved": False,
    "tests_status": {
        "FAIL_TO_PASS": {
            "success": ["tests/test_fields.py::TestMetadata::test_a"],
            "failure": [
                "tests/test_fields.py::TestMetadata::test_b[String]",
                "tests/test_fields.py::TestMetadata::test_b[Integer]",
            ],
        },
        "PASS_TO_PASS": {"success": ["tests/test_fields.py::test_kept"], "failure": []},
    },
}

_ALL_F2P_PASS_BUT_REGRESSED = {
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": True,
    # The harness says not resolved because a pass-to-pass test broke, even
    # though every fail-to-pass test passes.
    "resolved": False,
    "tests_status": {
        "FAIL_TO_PASS": {"success": ["t::a", "t::b"], "failure": []},
        "PASS_TO_PASS": {"success": ["t::kept"], "failure": ["t::broke"]},
    },
}

_COLLECTION_CRASH = {
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": False,
    "resolved": False,
    "infra_failure": True,
    "infra_failure_reason": "network_unreachable",
}

_NO_PATCH = {
    "patch_is_None": True,
    "patch_exists": False,
    "patch_successfully_applied": False,
    "resolved": False,
}


def test_y_is_the_fraction_of_fail_to_pass_tests_that_pass() -> None:
    outcome = fail_to_pass_outcome(_PARTIAL)
    assert outcome.f2p_success_count == 1
    assert outcome.f2p_failure_count == 2
    assert outcome.f2p_denominator == 3
    assert outcome.y == 1 / 3
    assert outcome.graded is True
    assert outcome.ungraded_reason == ""


def test_empty_denominator_scores_zero_not_the_official_perfect_score() -> None:
    """A run that graded no fail-to-pass test must score y=0, never y=1.

    `swebench.harness.grading.compute_fail_to_pass` returns 1 when the
    denominator is 0. That case means the patch never applied or the log never
    parsed -- the run produced no evidence about the bug -- so the official
    value would raise an arm's mean outcome in exactly the runs where the arm
    produced nothing, silently and without any error. Every empty-denominator
    shape below must therefore yield 0.0 and graded=False.
    """
    for payload in (_COLLECTION_CRASH, _NO_PATCH, {}, None):
        outcome = fail_to_pass_outcome(payload)
        assert outcome.y == 0.0, payload
        assert outcome.graded is False, payload
        assert outcome.f2p_denominator == 0, payload

    applied_but_nothing_graded = {
        "patch_exists": True,
        "patch_successfully_applied": True,
        "resolved": False,
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": []},
            "PASS_TO_PASS": {"success": [], "failure": []},
        },
    }
    outcome = fail_to_pass_outcome(applied_but_nothing_graded)
    assert outcome.y == 0.0
    assert outcome.graded is False


def test_ungraded_reason_separates_the_empty_denominator_shapes() -> None:
    assert fail_to_pass_outcome(None).ungraded_reason == UNGRADED_NO_REPORT
    assert fail_to_pass_outcome(_NO_PATCH).ungraded_reason == UNGRADED_NO_PATCH
    assert fail_to_pass_outcome(_COLLECTION_CRASH).ungraded_reason == UNGRADED_PATCH_NOT_APPLIED
    applied_empty = {
        "patch_exists": True,
        "patch_successfully_applied": True,
        "tests_status": {"FAIL_TO_PASS": {"success": [], "failure": []}},
    }
    assert fail_to_pass_outcome(applied_empty).ungraded_reason == UNGRADED_NO_FAIL_TO_PASS_TESTS


def test_full_fail_to_pass_is_not_reported_as_resolved() -> None:
    outcome = fail_to_pass_outcome(_ALL_F2P_PASS_BUT_REGRESSED)
    assert outcome.y == 1.0
    assert outcome.resolved is False
    assert outcome.p2p_failure_count == 1


def test_resolved_is_read_from_the_harness_not_derived_from_y() -> None:
    resolved_report = dict(_ALL_F2P_PASS_BUT_REGRESSED)
    resolved_report["resolved"] = True
    resolved_report["tests_status"] = {
        "FAIL_TO_PASS": {"success": ["t::a", "t::b"], "failure": []},
        "PASS_TO_PASS": {"success": ["t::kept"], "failure": []},
    }
    assert fail_to_pass_outcome(resolved_report).resolved is True
    # A partial outcome is never resolved even though it is graded.
    assert fail_to_pass_outcome(_PARTIAL).resolved is False


def test_raw_counts_survive_alongside_the_ratio() -> None:
    payload = fail_to_pass_outcome(_PARTIAL).as_dict()
    assert payload["f2p_success_count"] == 1
    assert payload["f2p_failure_count"] == 2
    assert payload["f2p_denominator"] == 3
    assert payload["y"] == 1 / 3
    # The counts must be recoverable from the stored row on their own.
    assert payload["f2p_success_count"] + payload["f2p_failure_count"] == payload["f2p_denominator"]


def test_malformed_tests_status_does_not_raise() -> None:
    for broken in (
        {"patch_successfully_applied": True, "tests_status": []},
        {"patch_successfully_applied": True, "tests_status": {"FAIL_TO_PASS": "nope"}},
        {"patch_successfully_applied": True, "tests_status": {"FAIL_TO_PASS": {"success": "nope"}}},
    ):
        outcome = fail_to_pass_outcome(broken)
        assert outcome.y == 0.0
        assert outcome.graded is False


def test_outcome_from_report_file_reads_the_harness_layout(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"astropy__astropy-12907": _PARTIAL}), encoding="utf-8")
    assert outcome_from_report_file(report, "astropy__astropy-12907").y == 1 / 3
    # A single-entry file does not need the instance id spelled out.
    assert outcome_from_report_file(report).y == 1 / 3
    # A named instance that is not in the file is ungraded, not another entry.
    assert outcome_from_report_file(report, "other__other-1").graded is False


def test_missing_or_unparseable_report_file_is_ungraded(tmp_path: Path) -> None:
    assert load_instance_report(tmp_path / "absent.json") is None
    broken = tmp_path / "report.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_instance_report(broken) is None
    assert outcome_from_report_file(broken).y == 0.0


def test_gold_denominator_check_flags_a_silently_shrunken_denominator() -> None:
    """A short denominator is the swebench 4.x SKIPPED symptom, not a clean run.

    The paper counts a skipped fail-to-pass test as unresolved, which holds only
    while every gold test lands in success or failure. If tests fall out of both
    lists the ratio was taken over a smaller population than the paper claims.
    """
    outcome = fail_to_pass_outcome(_PARTIAL)
    ok = gold_denominator_check(outcome, ["t::a", "t::b", "t::c"])
    assert ok["matches"] is True
    assert ok["missing_from_denominator"] == 0

    short = gold_denominator_check(outcome, ["t::a", "t::b", "t::c", "t::skipped"])
    assert short["matches"] is False
    assert short["missing_from_denominator"] == 1


def test_gold_denominator_check_reports_not_checked_rather_than_fine() -> None:
    outcome = fail_to_pass_outcome(_PARTIAL)
    unchecked = gold_denominator_check(outcome, None)
    assert unchecked["matches"] is None
    assert unchecked["gold_count"] is None
