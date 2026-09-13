from __future__ import annotations

import pytest
from swe_v1_prolite_runner_test_support import _remote_namespace, _seed_remote_completed_generation, _write_jsonl

from opencollab_eval.engine.swe_generation_proof import reusable_intrinsic_role_failures


def test_blocked_workflow_with_agent_failures_and_proven_candidate_continues_to_eval(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="blocked",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[
            {"exception_type": "ContextOverflow", "label": "coder:r1"},
            {"exception_type": "SessionStopped", "label": "final-verifier:r1"},
        ],
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["workflow_status"] == "blocked"
    assert result["submission_eligible"] is True
    assert result["generation_attempt_count"] == 1

@pytest.mark.parametrize("failures", [
    None, {}, [{"label": "reviewer", "status": "failed"}],
    [{"exception_type": "SessionStopped", "status_code": 503}],
    [{"exception_type": "ContextOverflow", "provider_error_type": "rate_limit"}],
    [{"exception_type": "RuntimeError"}],
])
def test_unknown_or_provider_failures_still_prevent_completed_generation_reuse(failures):
    assert not reusable_intrinsic_role_failures(failures)
