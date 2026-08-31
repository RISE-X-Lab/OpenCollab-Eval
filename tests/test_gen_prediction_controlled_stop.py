from __future__ import annotations

import pytest

from opencollab_eval.generation import gen_prediction_safe_output as safe_output


@pytest.mark.parametrize(
    "reason",
    ["budget_exceeded", "step_limit_exceeded", "context_overflow"],
)
def test_trusted_controlled_stop_patch_uses_timeout_contract(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    patch = "+fixed\n"
    monkeypatch.setattr(
        safe_output,
        "current_generation_proof_valid",
        lambda metrics, candidate: True,
    )
    metrics = {
        "workflow_status": reason,
        "execution_quiesced": True,
        "submission_eligible": True,
    }

    safe_output.complete_single_agent_integrity(
        metrics,
        patch=patch,
        patch_extraction_succeeded=True,
    )
    _prediction, metric = safe_output.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics=metrics,
    )

    assert metric["workflow_status"] == "done_with_timeout_patch"
    assert metric["runner_returncode"] == 124
    assert safe_output.metrics_have_completed_identity(metric, patch)


def test_empty_controlled_stop_patch_remains_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        safe_output,
        "current_generation_proof_valid",
        lambda metrics, candidate: True,
    )
    metrics = {
        "workflow_status": "budget_exceeded",
        "execution_quiesced": True,
        "submission_eligible": True,
    }

    safe_output.normalize_trusted_extraction_status(metrics, "")
    safe_output.complete_single_agent_integrity(
        metrics,
        patch="",
        patch_extraction_succeeded=True,
    )

    assert metrics["workflow_status"] == "budget_exceeded"
    assert metrics["submission_eligible"] is False
