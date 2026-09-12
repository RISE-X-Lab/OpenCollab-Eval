"""Eval-only candidate isolation and historical generation observation."""

from __future__ import annotations

from typing import Any

from opencollab_eval.engine import swe_v1_remote_state as state
from opencollab_eval.engine.swe_v1_remote_candidate_isolation import (
    isolate_eval_only_candidate,
)
from opencollab_eval.engine.swe_v1_remote_generation import (
    eval_only_candidate_identity_error,
    generation_done,
    generation_done_result,
    historical_generation_identity_status,
)


def prepare_eval_only_candidate_isolation(
    selected: list[dict[str, Any]],
) -> dict[str, Any] | None:
    source = state.eval_only_source_base_run_dir
    if source is None:
        return None
    if len(selected) != 1 or selected[0]["instance_id"] != state.expected_task:
        raise ValueError("eval-only isolation task selection mismatch")
    return isolate_eval_only_candidate(
        source_base_run_dir=source,
        target_base_run_dir=state.base_run_dir,
        task=state.expected_task,
        record_id=state.expected_record_id,
        source_patch_sha256=state.expected_source_patch_sha256,
        eval_patch_sha256=state.expected_eval_patch_sha256,
    )


def observe_eval_only_generation(row: dict[str, Any]) -> dict[str, Any]:
    task = row["instance_id"]
    done, prediction, metric, pairing = generation_done(
        state.base_run_dir / task,
        task,
        require_identity=False,
    )
    if not done:
        return {
            "status": "skipped_no_generation_patch",
            "task": task,
            "pairing": pairing,
            "eval_only": True,
        }
    generated = generation_done_result(
        task,
        prediction,
        metric,
        pairing,
        eval_only=True,
        artifact_identity_status=historical_generation_identity_status(
            prediction,
            metric,
            task,
        ),
    )
    return eval_only_candidate_identity_error(generated) or generated


def candidate_isolation_summary(
    isolation: dict[str, Any] | None,
) -> dict[str, Any]:
    source = state.eval_only_source_base_run_dir
    return {
        "eval_only_source_base_run_dir": str(source) if source is not None else "",
        "candidate_isolation": isolation,
    }


__all__ = [
    "candidate_isolation_summary",
    "observe_eval_only_generation",
    "prepare_eval_only_candidate_isolation",
]
