from __future__ import annotations

import pytest

from opencollab_eval.generation import gen_prediction_safe_output as safe_output


@pytest.mark.parametrize(
    "reason",
    [
        "budget_exceeded",
        "budget exceeded: 100 tokens used",
        "budget exhausted before model call: no output headroom",
        "team budget exceeded: aggregate spend reached the global cap",
        "step_limit_exceeded",
        "step limit reached: 4 steps",
        "context_overflow",
        "context overflow: prompt exceeds the model context window",
    ],
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
        "workflow_status": "budget exceeded: 100 tokens used",
        "execution_quiesced": True,
        "submission_eligible": True,
    }

    safe_output.normalize_trusted_extraction_status(metrics, "")
    safe_output.complete_single_agent_integrity(
        metrics,
        patch="",
        patch_extraction_succeeded=True,
    )

    assert metrics["workflow_status"] == "budget exceeded: 100 tokens used"
    assert metrics["submission_eligible"] is False


@pytest.mark.parametrize("reason", ["cancelled", "loop block limit reached: 3"])
def test_uncontrolled_stop_patch_keeps_original_status(
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

    assert metrics["workflow_status"] == reason
