from __future__ import annotations

import pytest

from opencollab_eval.engine.swe_eval_outcome import (
    EvaluationOutcome,
    TargetOutcome,
    classify_evaluation,
    target_evidence_outcome,
)


def _evidence(status: int, **updates: object) -> dict[str, object]:
    value = {
        "status": status,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "artifact_safe": True,
        "target_proof_matches_plan": False,
        "target_failure_proof_matches_plan": False,
        "target_skip_proof_matches_plan": False,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (_evidence(0, target_proof_matches_plan=True), TargetOutcome.PASSED),
        (_evidence(1, target_proof_matches_plan=True), TargetOutcome.PASSED),
        (_evidence(1, target_failure_proof_matches_plan=True), TargetOutcome.CANDIDATE_FAILED),
        (_evidence(0, target_skip_proof_matches_plan=True), TargetOutcome.SKIPPED),
        (_evidence(124, target_proof_matches_plan=True), TargetOutcome.UNKNOWN),
        (_evidence(127, target_proof_matches_plan=True), TargetOutcome.UNKNOWN),
        (
            _evidence(
                1,
                target_proof_matches_plan=True,
                target_failure_proof_matches_plan=True,
            ),
            TargetOutcome.UNKNOWN,
        ),
        (_evidence(1, command_matches_plan=False), TargetOutcome.UNKNOWN),
        (_evidence(1, artifact_safe=False), TargetOutcome.UNKNOWN),
    ],
)
def test_target_evidence_has_four_semantic_outcomes(
    evidence: dict[str, object],
    expected: TargetOutcome,
) -> None:
    assert target_evidence_outcome(evidence) is expected


@pytest.mark.parametrize(
    ("evidence", "reasons", "patch_failed", "expected"),
    [
        (
            [_evidence(0, target_proof_matches_plan=True)],
            (),
            False,
            EvaluationOutcome.RESOLVED,
        ),
        (
            [_evidence(1, target_failure_proof_matches_plan=True)],
            (),
            False,
            EvaluationOutcome.UNRESOLVED,
        ),
        (
            [_evidence(0, target_skip_proof_matches_plan=True)],
            (),
            False,
            EvaluationOutcome.UNRESOLVED,
        ),
        (
            [_evidence(124, target_proof_matches_plan=True)],
            (),
            False,
            EvaluationOutcome.TECHNICAL_FAILURE,
        ),
        (
            [],
            (),
            True,
            EvaluationOutcome.UNRESOLVED,
        ),
        (
            [_evidence(1, target_failure_proof_matches_plan=True)],
            ("candidate_identity",),
            False,
            EvaluationOutcome.TECHNICAL_FAILURE,
        ),
        (
            [],
            ("projection_runtime",),
            True,
            EvaluationOutcome.TECHNICAL_FAILURE,
        ),
    ],
)
def test_evaluation_outcome_uses_asymmetric_proof(
    evidence: list[dict[str, object]],
    reasons: tuple[str, ...],
    patch_failed: bool,
    expected: EvaluationOutcome,
) -> None:
    decision = classify_evaluation(
        evidence=evidence,
        prerequisite_reasons=reasons,
        candidate_application_failed=patch_failed,
    )

    assert decision.outcome is expected
    assert decision.resolved is (expected is EvaluationOutcome.RESOLVED)


def test_one_bound_failure_is_conclusive_when_a_later_batch_is_unknown() -> None:
    decision = classify_evaluation(
        evidence=[
            _evidence(1, target_failure_proof_matches_plan=True),
            _evidence(99, command_matches_plan=False, artifact_safe=False),
        ]
    )

    assert decision.outcome is EvaluationOutcome.UNRESOLVED
    assert decision.technical_reasons == ()
