"""Retry and attempt-identity orchestration for direct evaluation."""

# ruff: noqa: F403, F405

from opencollab_eval.engine.swe_v1_remote_eval_patch import *
from opencollab_eval.engine.swe_v1_remote_generation import *
from opencollab_eval.engine.swe_v1_remote_records import *
from opencollab_eval.engine.swe_v1_remote_state import *


def eval_for_task_with_retries(row, eval_once):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    done, prediction, metric, pairing = generation_done(
        run_dir,
        task,
        require_identity=not eval_only,
    )
    if not done:
        result = dict(eval_once(row))
        result["attempt_count"] = 0
        result["max_eval_attempts"] = max_eval_attempts
        return result
    patch_selection = verified_plan_patch_selection(row, prediction, metric)
    expected_eval_patch_sha256 = (
        str(patch_selection.get("eval_patch_sha256") or "")
        if patch_selection is not None
        else ""
    )
    expected_eval_image_id = (
        str(patch_selection.get("image_id") or "")
        if patch_selection is not None
        else ""
    )
    persisted_attempts = eval_attempt_count(
        run_dir,
        prediction,
        task,
        expected_eval_patch_sha256=expected_eval_patch_sha256,
        expected_eval_image_id=expected_eval_image_id,
    )
    if persisted_attempts >= max_eval_attempts:
        previous = load_json(run_dir / eval_dir_name / "summary.json")
        status = (
            "eval_done"
            if eval_summary_matches_prediction(
                previous,
                prediction,
                task,
                expected_eval_patch_sha256=expected_eval_patch_sha256,
                expected_eval_image_id=expected_eval_image_id,
            )
            else "technical_eval_failed"
        )
        return {
            "status": status,
            "task": task,
            "summary": previous,
            "pairing": pairing,
            "executed": False,
            "retry_budget_exhausted": status != "eval_done",
            "attempt_count": persisted_attempts,
            "max_eval_attempts": max_eval_attempts,
        }
    attempts = []
    retry_statuses = {"technical_eval_failed", "blocked_missing_eval_image"}
    for attempt_index in range(max_eval_attempts - persisted_attempts):
        if patch_selection is None or attempt_index > 0 and not patch_selection.get("ok"):
            patch_selection = verified_plan_patch_selection(row, prediction, metric)
        if patch_selection is not None and patch_selection.get("ok"):
            expected_eval_patch_sha256 = str(patch_selection.get("eval_patch_sha256") or "")
            expected_eval_image_id = str(patch_selection.get("image_id") or "")
        result = dict(eval_once(row, patch_selection))
        attempts.append(result)
        if result.get("status") not in retry_statuses:
            break
        if not eval_retry_cleanup_safe(result):
            break
        current_attempts = eval_attempt_count(
            run_dir,
            prediction,
            task,
            expected_eval_patch_sha256=expected_eval_patch_sha256,
            expected_eval_image_id=expected_eval_image_id,
        )
        if current_attempts >= max_eval_attempts:
            break
        append_jsonl(
            base_run_dir / "events.jsonl",
            {
                "time": now(),
                "phase": "eval_retry",
                "task": task,
                "attempt": current_attempts + 1,
                "previous_status": result.get("status"),
                "technical_reasons": (
                    (result.get("summary") or {}).get("technical_reasons", [])
                ),
            },
        )
    final = dict(attempts[-1])
    final["attempt_count"] = eval_attempt_count(
        run_dir,
        prediction,
        task,
        expected_eval_patch_sha256=expected_eval_patch_sha256,
        expected_eval_image_id=expected_eval_image_id,
    )
    final["max_eval_attempts"] = max_eval_attempts
    if len(attempts) > 1:
        final["attempts"] = attempts
    return final


__all__ = ["eval_for_task_with_retries"]
