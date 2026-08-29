"""Whether a run's workspace is read must not depend on how the run stopped.

Split out of ``test_gen_prediction_single_agent`` only because that file is at
the repository's line ceiling; these belong to the same contract.
"""

from __future__ import annotations

import asyncio

from gen_prediction_single_agent_support import (
    RecordingRuntime,
    _agent_config,
    _reserve_empty_artifact_dir,
    _runtime_result,
)

from opencollab_eval.generation import gen_prediction as gp


def test_a_budget_stopped_run_still_has_its_workspace_read(monkeypatch, tmp_path):
    """Running out of tokens must not throw away the work already on disk.

    Eligibility used to also require ``workflow_status`` to be ``done`` or
    ``done_with_timeout_patch``. A wall-clock timeout maps to the second, so a
    run that ran out of *time* kept its patch; a run that ran out of *tokens*
    maps to the raw stop reason, matched neither, and was reported as "empty
    patch (agent made no tracked changes)" while its edits sat in /testbed.

    The arm this one is compared against never did that: the workflow/team path
    gates extraction on the container evidence, not on the terminal reason, and
    an identically budget-stopped team run kept its patch. So the outcome
    measure differed by arm for a reason that is not what the comparison is
    about, which is the one thing this harness may not do.
    """
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(phase="budget_exceeded", step_count=38)
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 60, 1000, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["candidate_probe_eligible"] is True
    # The run is still reported for what it was. Only whether the workspace is
    # read changed; the status, and the non-zero exit code it produces, did not.
    assert metrics["workflow_status"] == "budget_exceeded"
    assert metrics["wall_clock_timeout"] is False
    assert gp.metrics_have_completed_identity(metrics, "+candidate") is False


def test_a_stop_reason_the_harness_has_never_seen_is_still_eligible(monkeypatch, tmp_path):
    """The rule is about the session quiescing, not about a list of reasons.

    Enumerating the acceptable stop reasons is how the budget case was missed
    in the first place; a step ceiling, a loop block or a context overflow
    would each have been missed the same way.
    """
    _reserve_empty_artifact_dir(monkeypatch, tmp_path)
    runtime = RecordingRuntime(
        _runtime_result(phase="step_limit_exceeded")
    )

    metrics = asyncio.run(
        gp.run_agent(
            "task", "cid", _agent_config(), 60, 1000, 1, artifact_root=tmp_path, runtime=runtime
        )
    )

    assert metrics["candidate_probe_eligible"] is True
