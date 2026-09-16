"""Deterministic post-patch gates for the validation council workflow."""

from __future__ import annotations

from copy import deepcopy

import pytest

from opencollab_eval.workflows._validation_council_lean_official_impl import _run_critic
from opencollab_eval.workflows._validation_council_lean_official_post_patch import (
    _apply_risk_critic,
    _enforce_post_patch_gates,
    _has_actionable_retry,
)


def _passing_evidence() -> dict:
    return {
        "diff": {
            "changed_files": ["widget.py"],
            "unexpected_files": [],
            "summary": "guard empty input",
        },
        "validated_patch_sha256": "",
        "contract_checks": [
            {
                "contract_id": "C1",
                "status": "pass",
                "command": "python -c 'check_empty()'",
                "evidence": "returned empty widget",
            }
        ],
        "coverage_checks": [
            {
                "case_id": "B1",
                "status": "pass",
                "command": "python -c 'check_empty()'",
                "evidence": "returned empty widget",
            }
        ],
        "baseline_replay": [],
        "risk_checks": [],
        "failure_classifications": [],
        "remaining_defects": [],
        "retry_brief": {"must_fix": [], "do_not_change": []},
        "critic_reason": "",
        "verdict": "PASS",
        "findings": "all checks passed",
        "allowed_patch_paths": ["widget.py"],
        "protected_paths": [],
        "actual_disallowed_changed_files": [],
    }


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda evidence: (
                evidence["contract_checks"][0].update(command=""),
                evidence["coverage_checks"][0].update(command=""),
            ),
            "successful executable",
        ),
        (lambda evidence: evidence["diff"].update(changed_files=[]), "changed source files"),
        (
            lambda evidence: (
                evidence["diff"]["changed_files"].append("tests/test_widget.py"),
                evidence["diff"].update(unexpected_files=["tests/test_widget.py"]),
            ),
            "Unexpected or disallowed",
        ),
        (
            lambda evidence: evidence["risk_checks"].append(
                {
                    "risk": "None handling",
                    "contract_ids": ["C1"],
                    "command": "python -c 'check_none()'",
                    "result": "fail",
                    "evidence": "AttributeError",
                }
            ),
            "unresolved failing",
        ),
        (
            lambda evidence: evidence.update(remaining_defects=["None still crashes"]),
            "unresolved defects",
        ),
    ],
)
def test_deterministic_pass_gates_reject_unsupported_claims(mutator, message):
    evidence = _passing_evidence()
    mutator(evidence)

    checked = _enforce_post_patch_gates(
        evidence,
        source_changed=True,
        critic_may_be_requested=True,
    )

    assert checked["verdict"] == "FAIL"
    assert message in checked["findings"]


def test_pass_gate_requires_source_diff():
    checked = _enforce_post_patch_gates(
        _passing_evidence(),
        source_changed=False,
        critic_may_be_requested=True,
    )

    assert checked["verdict"] == "FAIL"
    assert "No tracked source change" in checked["findings"]


def test_advisory_behavior_gap_does_not_override_executable_pass():
    evidence = _passing_evidence()
    evidence["coverage_checks"] = []

    checked = _enforce_post_patch_gates(
        evidence,
        source_changed=True,
        critic_may_be_requested=True,
    )

    assert checked["verdict"] == "PASS"


def test_advisory_coverage_is_not_required_for_a_pass():
    evidence = _passing_evidence()
    evidence["coverage_checks"] = []
    merged = _apply_risk_critic(
        evidence,
        {
            "recommendation": "PASS",
            "risks": [],
            "coverage_reviews": [
                {
                    "case_id": "B1",
                    "status": "pass",
                    "command": "",
                    "evidence": "not actually executed",
                }
            ],
            "environment_findings": [],
            "evidence_gaps": [],
            "summary": "claimed complete",
        },
    )
    checked = _enforce_post_patch_gates(
        merged,
        source_changed=True,
        critic_may_be_requested=False,
        critic_confirmed=True,
    )

    assert checked["verdict"] == "PASS"


def test_retry_requires_observed_failure_or_precise_critic_finding():
    generic_gap = _passing_evidence()
    generic_gap["verdict"] = "FAIL"
    generic_gap["retry_brief"]["must_fix"] = [
        {
            "problem": "case not checked",
            "evidence": "no result",
            "failed_command": "",
            "location": "widget.py:parse",
            "distinguishing_probe": "pytest tests/test_widget.py",
        }
    ]
    observed_failure = deepcopy(generic_gap)
    observed_failure["failure_classifications"] = [
        {
            "command": "pytest tests/test_widget.py",
            "classification": "unresolved",
        }
    ]
    observed_failure["retry_brief"]["must_fix"][0]["failed_command"] = "pytest tests/test_widget.py"
    critic_finding = deepcopy(generic_gap)
    critic_finding["retry_brief"]["must_fix"][0]["origin"] = "critic"

    assert _has_actionable_retry(generic_gap) is False
    assert _has_actionable_retry(observed_failure) is True
    assert _has_actionable_retry(critic_finding) is True


def test_pass_gate_rejects_patch_fingerprint_mismatch():
    evidence = _passing_evidence()
    evidence["validated_patch_sha256"] = "old-patch"

    checked = _enforce_post_patch_gates(
        evidence,
        source_changed=True,
        critic_may_be_requested=True,
        patch_fingerprint="current-patch",
    )

    assert checked["verdict"] == "FAIL"
    assert "current patch fingerprint" in checked["findings"]


def test_expected_obsolete_failure_requires_independent_critic_confirmation():
    evidence = _passing_evidence()
    evidence["risk_checks"] = [
        {
            "risk": "old assertion",
            "contract_ids": ["C1"],
            "command": "pytest tests/test_widget.py",
            "result": "fail",
            "evidence": "assertion expects the old output",
        }
    ]
    evidence["failure_classifications"] = [
        {
            "command": "pytest tests/test_widget.py",
            "classification": "expected_obsolete",
            "baseline_test_id": "T1",
            "failure_signature": "assertion mismatch",
            "evidence": "the assertion directly contradicts the requested behavior",
        }
    ]

    before_critic = _enforce_post_patch_gates(
        deepcopy(evidence),
        source_changed=True,
        critic_may_be_requested=True,
    )
    after_critic = _enforce_post_patch_gates(
        deepcopy(evidence),
        source_changed=True,
        critic_may_be_requested=False,
        critic_confirmed=True,
    )

    assert before_critic["verdict"] == "NEEDS_CRITIC"
    assert after_critic["verdict"] == "PASS"


def test_environment_failure_requires_observed_baseline_match():
    evidence = _passing_evidence()
    evidence["risk_checks"] = [
        {
            "risk": "runner dependency",
            "contract_ids": ["C1"],
            "command": "pytest tests/test_widget.py",
            "result": "fail",
            "evidence": "ModuleNotFoundError: widget_runtime",
        }
    ]
    evidence["failure_classifications"] = [
        {
            "command": "pytest tests/test_widget.py",
            "classification": "environmental",
            "baseline_test_id": "ENV1",
            "failure_signature": "ModuleNotFoundError: widget_runtime",
            "evidence": "same missing module on the unmodified tree",
        }
    ]
    baseline = {
        "classifications": [
            {
                "test_id": "ENV1",
                "status": "base_environment_failure",
                "command": "pytest tests/test_widget.py",
                "observed": True,
                "evidence": "ModuleNotFoundError",
                "failure_signature": "ModuleNotFoundError: widget_runtime",
            }
        ]
    }
    wrong_signature = deepcopy(baseline)
    wrong_signature["classifications"][0]["failure_signature"] = "other failure"

    unsupported = _enforce_post_patch_gates(
        deepcopy(evidence),
        source_changed=True,
        critic_may_be_requested=True,
    )
    mismatched = _enforce_post_patch_gates(
        deepcopy(evidence),
        source_changed=True,
        critic_may_be_requested=True,
        baseline_triage=wrong_signature,
    )
    supported = _enforce_post_patch_gates(
        deepcopy(evidence),
        source_changed=True,
        critic_may_be_requested=True,
        baseline_triage=baseline,
    )

    assert unsupported["verdict"] == "FAIL"
    assert mismatched["verdict"] == "FAIL"
    assert "matching observed baseline" in unsupported["findings"]
    assert supported["verdict"] == "BLOCKED"


def test_protected_path_policy_without_actual_changed_file_is_not_a_failure():
    evidence = _passing_evidence()
    evidence["protected_paths"] = ["tests/", "setup.cfg"]

    checked = _enforce_post_patch_gates(
        evidence,
        source_changed=True,
        critic_may_be_requested=True,
        actual_changed_files=["widget.py"],
        actual_disallowed_changed_files=[],
    )

    assert checked["verdict"] == "PASS"
    assert checked["disallowed_patch_paths"] == []


@pytest.mark.asyncio
async def test_critic_source_mutation_invalidates_its_report_inside_candidate():
    initial_diff = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1 +1 @@
-broken = True
+broken = False
"""
    mutated_diff = initial_diff + """diff --git a/extra.py b/extra.py
--- /dev/null
+++ b/extra.py
@@ -0,0 +1 @@
+unexpected = True
"""

    class MutatingCriticCtx:
        def __init__(self):
            self.current_diff = initial_diff

        async def diff(self):
            return self.current_diff

        async def agent(self, *_args, **_kwargs):
            self.current_diff = mutated_diff
            return {
                "recommendation": "PASS",
                "risks": [],
                "coverage_reviews": [],
                "environment_findings": [],
                "evidence_gaps": [],
                "summary": "looks good",
            }

    ctx = MutatingCriticCtx()
    report = await _run_critic(
        ctx,
        prompt="review",
        label="risk-critic:r1",
        injected_test_paths=[],
    )

    assert report["recommendation"] == "INCONCLUSIVE"
    assert "altered the candidate source diff" in report["summary"]
    assert ctx.current_diff == mutated_diff
