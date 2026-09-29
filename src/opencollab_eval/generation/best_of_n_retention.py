"""Keep a Best-of-N candidate's owned source when publication is interrupted."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencollab_eval.engine.async_runtime import add_exception_note

from .candidate_retention import retain_failed_candidate
from .gen_prediction_patch import TrustedPatchBaseline


def retain_best_of_n_source(
    *,
    run_dir: Path,
    instance: dict,
    image: str,
    index: int,
    cid: str | None,
    name: str,
    baseline: Any,
    generation_image_id: str | None,
    metrics: dict,
    error: BaseException | None,
    reason: str,
    mark_kept: Callable[[Path, str], None],
    quiesce: Callable[[str], None],
    isolate: Callable[[str], str],
    persist_failure: Callable[..., None],
) -> None:
    """Preserve the owned workspace and trusted baseline for later extraction."""
    record_error = error or RuntimeError(metrics.get("error") or "trusted patch extraction incomplete")
    evidence: dict[str, object] = {"container_retained": False, "solver_quiesced": False}
    if cid is not None:
        try:
            mark_kept(run_dir, cid)
            evidence["container_retained"] = True
        except BaseException as retain_error:
            add_exception_note(
                record_error,
                f"container preservation failed: {type(retain_error).__name__}: {retain_error}",
            )
        try:
            quiesce(cid)
            evidence["solver_quiesced"] = True
        except BaseException as quiescence_error:
            add_exception_note(
                record_error,
                f"solver quiescence failed: {type(quiescence_error).__name__}: {quiescence_error}",
            )
            try:
                evidence["container_isolated"] = isolate(cid)
            except BaseException as isolation_error:
                add_exception_note(
                    record_error,
                    f"container isolation failed: {type(isolation_error).__name__}: {isolation_error}",
                )
    if cid is not None and isinstance(baseline, TrustedPatchBaseline):
        from . import gen_prediction as gp

        try:
            receipt = retain_failed_candidate(
                gp,
                run_dir=run_dir,
                cid=cid,
                name=name,
                baseline=baseline,
                instance=instance,
                image=image,
                generation_image_id=generation_image_id,
                metrics=metrics,
                generation_error=error,
                workflow_log_dir=None,
                task_id=None,
                trajectory_path=metrics.get("trajectory_path"),
                reason=reason,
            )
            evidence["candidate_recovery_path"] = str(receipt)
        except BaseException as retention_error:
            add_exception_note(
                record_error,
                f"candidate recovery entry failed: {type(retention_error).__name__}: {retention_error}",
            )
            evidence["candidate_retention_error"] = str(retention_error)
    try:
        persist_failure(
            run_dir,
            instance_id=instance["instance_id"],
            phase=f"best-of-n_candidate_{index}_persistence",
            error=record_error,
            evidence=evidence,
        )
    except BaseException as persist_error:
        add_exception_note(
            record_error,
            f"candidate failure record could not be written: {type(persist_error).__name__}: {persist_error}",
        )
