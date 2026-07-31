"""Semantic outcome classification for one bound SWE candidate."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from opencollab_eval.engine.eval_candidate_projection import (
    candidate_rejection_is_conclusive,
)


class TargetOutcome(str, Enum):
    """Outcome proved for one planned test-command batch."""

    PASSED = "passed"
    CANDIDATE_FAILED = "candidate_failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class EvaluationOutcome(str, Enum):
    """Final semantic outcome for one candidate."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    TECHNICAL_FAILURE = "technical_failure"


@dataclass(frozen=True)
class VerdictDecision:
    outcome: EvaluationOutcome
    resolved: bool
    technical_reasons: tuple[str, ...]
    basis: tuple[str, ...]


def target_evidence_outcome(item: dict[str, Any]) -> TargetOutcome:
    """Interpret parser-backed evidence without inferring from log appearance."""
    status = item.get("status")
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or item.get("command_matches_plan") is not True
        or item.get("log_artifact_safe") is not True
        or item.get("artifact_safe") is not True
    ):
        return TargetOutcome.UNKNOWN
    passed = item.get("target_proof_matches_plan") is True
    failed = item.get("target_failure_proof_matches_plan") is True
    skipped = item.get("target_skip_proof_matches_plan") is True
    if sum((passed, failed, skipped)) != 1:
        return TargetOutcome.UNKNOWN
    if passed and status in {0, 1}:
        return TargetOutcome.PASSED
    if failed and status != 0:
        return TargetOutcome.CANDIDATE_FAILED
    if skipped and status == 0:
        return TargetOutcome.SKIPPED
    return TargetOutcome.UNKNOWN


def classify_evaluation(
    *,
    evidence: Iterable[dict[str, Any]],
    prerequisite_reasons: Iterable[str] = (),
    candidate_application_failed: bool = False,
) -> VerdictDecision:
    """Apply asymmetric proof rules to one bound candidate."""
    reasons = tuple(dict.fromkeys(str(reason) for reason in prerequisite_reasons if reason))
    if reasons:
        return VerdictDecision(
            EvaluationOutcome.TECHNICAL_FAILURE,
            False,
            reasons,
            ("evaluation_prerequisite_failed",),
        )
    if candidate_application_failed:
        return VerdictDecision(
            EvaluationOutcome.UNRESOLVED,
            False,
            (),
            ("candidate_patch_could_not_be_applied",),
        )
    outcomes = tuple(target_evidence_outcome(item) for item in evidence)
    if any(
        outcome in {TargetOutcome.CANDIDATE_FAILED, TargetOutcome.SKIPPED}
        for outcome in outcomes
    ):
        basis = tuple(
            dict.fromkeys(
                "declared_target_failed"
                if outcome is TargetOutcome.CANDIDATE_FAILED
                else "declared_target_skipped"
                for outcome in outcomes
                if outcome in {TargetOutcome.CANDIDATE_FAILED, TargetOutcome.SKIPPED}
            )
        )
        return VerdictDecision(EvaluationOutcome.UNRESOLVED, False, (), basis)
    if outcomes and all(outcome is TargetOutcome.PASSED for outcome in outcomes):
        return VerdictDecision(
            EvaluationOutcome.RESOLVED,
            True,
            (),
            ("all_declared_targets_passed",),
        )
    return VerdictDecision(
        EvaluationOutcome.TECHNICAL_FAILURE,
        False,
        ("target_outcome_unknown",),
        ("insufficient_semantic_test_evidence",),
    )


def _plan_evidence_mismatch(artifacts: dict[str, Any], prefix: str) -> bool:
    evidence = artifacts[f"{prefix}_evidence"]
    status = artifacts[f"{prefix}_status"]
    aggregate_status = next(
        (item["status"] for item in evidence if item["status"] != 0),
        0,
    )
    return not artifacts[f"{prefix}_execution_evidence_complete"] or bool(
        evidence and aggregate_status != status
    )


_UNRUN_TEST_ARTIFACT_RE = re.compile(
    r"missing:(?:f2p|p2p)\.batch_[0-9]{3}\."
    r"(?:command|exit|log|proof\.[A-Za-z0-9_.-]+\.jsonl)\Z"
)
_EXPECTED_CANDIDATE_REJECTION_ARTIFACT_RE = re.compile(
    r"(?:missing:runtime_dependencies\.json|"
    r"unsafe:(?:service_bootstrap|test_patch|f2p|p2p)\.log:FileNotFoundError|"
    r"missing:(?:f2p|p2p)\.batch_[0-9]{3}\."
    r"(?:command|exit|log|proof\.[A-Za-z0-9_.-]+\.jsonl))\Z"
)


def derive_eval_verdict(
    artifacts: dict[str, Any],
    *,
    docker_exit: int,
    cleanup_quiesced: bool,
    container_cleanup: dict[str, Any],
) -> dict[str, Any]:
    """Derive a verdict from identity-bound artifacts and semantic evidence."""
    evidence = [*artifacts["f2p_evidence"], *artifacts["p2p_evidence"]]
    operational_warnings = list(artifacts.get("operational_warnings") or [])
    container_stopped = docker_exit == 0 and cleanup_quiesced
    if not container_cleanup.get("ok") and container_stopped:
        operational_warnings.append("container_removal_failed_after_stop")
    target_outcomes = tuple(target_evidence_outcome(item) for item in evidence)
    conclusive_candidate_failure = any(
        outcome in {TargetOutcome.CANDIDATE_FAILED, TargetOutcome.SKIPPED}
        for outcome in target_outcomes
    )
    candidate_application_failed = bool(
        artifacts["model_status"] != 0
        and candidate_rejection_is_conclusive(
            artifacts.get("candidate_projection_failure")
        )
        and artifacts["base_commit_status"] == 0
        and artifacts["base_snapshot"]
        and artifacts["before_status"] == 0
        and artifacts["post_before_base_status"] == 0
        and docker_exit == 0
        and cleanup_quiesced
    )
    output_errors = [
        error
        for error in artifacts["output_artifact_errors"]
        if not (
            candidate_application_failed
            and _EXPECTED_CANDIDATE_REJECTION_ARTIFACT_RE.fullmatch(error)
            or conclusive_candidate_failure
            and _UNRUN_TEST_ARTIFACT_RE.fullmatch(error)
        )
    ]
    operational_warnings.extend(
        f"ignored_after_semantic_verdict:{error}"
        for error in artifacts["output_artifact_errors"]
        if error not in output_errors
    )
    reason_checks = (
        ("unsafe_or_missing_output_artifact", bool(output_errors)),
        ("docker_exit", docker_exit != 0),
        ("process_cleanup", not cleanup_quiesced),
        ("container_cleanup", not container_cleanup.get("ok") and not container_stopped),
        ("base_commit", artifacts["base_commit_status"] != 0),
        ("base_snapshot_integrity", not artifacts["base_snapshot"]),
        (
            "candidate_projection",
            not artifacts["candidate_projection"] and not candidate_application_failed,
        ),
        (
            "service_bootstrap",
            artifacts["service_status"] != 0 and not candidate_application_failed,
        ),
        ("before_repo", artifacts["before_status"] != 0),
        ("post_before_base_commit", artifacts["post_before_base_status"] != 0),
        (
            "model_patch",
            artifacts["model_status"] != 0 and not candidate_application_failed,
        ),
        (
            "test_patch",
            artifacts["test_status"] != 0 and not candidate_application_failed,
        ),
    )
    prerequisite_reasons = [reason for reason, active in reason_checks if active]
    decision = classify_evaluation(
        evidence=evidence,
        prerequisite_reasons=prerequisite_reasons,
        candidate_application_failed=candidate_application_failed,
    )
    if decision.outcome is EvaluationOutcome.TECHNICAL_FAILURE:
        if _plan_evidence_mismatch(artifacts, "f2p"):
            prerequisite_reasons.append("fail_to_pass_evidence")
        if _plan_evidence_mismatch(artifacts, "p2p"):
            prerequisite_reasons.append("pass_to_pass_evidence")
        decision = classify_evaluation(
            evidence=evidence,
            prerequisite_reasons=prerequisite_reasons,
        )
    technical_error = decision.outcome is EvaluationOutcome.TECHNICAL_FAILURE
    return {
        "outcome": decision.outcome.value,
        "outcome_basis": list(decision.basis),
        "technical_reasons": list(decision.technical_reasons),
        "technical_error": technical_error,
        "resolved": decision.resolved,
        "summary_status": "technical_eval_failed" if technical_error else "done",
        "output_artifact_errors": output_errors,
        "operational_warnings": list(dict.fromkeys(operational_warnings)),
    }


__all__ = [
    "EvaluationOutcome",
    "TargetOutcome",
    "VerdictDecision",
    "classify_evaluation",
    "derive_eval_verdict",
    "target_evidence_outcome",
]
