"""Failure-safe teardown helpers for one OpenHands generation attempt."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def cleanup_openhands_attempt(
    *,
    gp: Any,
    shutil_module: Any = shutil,
    trusted_baseline: object | None,
    evidence_dir: Path,
    openhands_dir: Path,
    run_dir: Path,
    cid: str,
    name: str,
    pending_required: bool,
    pending_path: Path | None,
    metrics: dict,
    patch: str,
    generation_error: BaseException | None,
    keep_container: bool,
) -> BaseException | None:
    """Run teardown steps independently and preserve the primary failure."""
    failures: list[tuple[BaseException, str]] = []

    def record(label: str, exc: BaseException) -> None:
        message = f"{label}: {type(exc).__name__}: {exc}"[:4000]
        failures.append((exc, message))
        values = metrics.setdefault("cleanup_errors", [])
        metrics["cleanup_errors"] = values if isinstance(values, list) else []
        metrics["cleanup_errors"].append(message)

    if trusted_baseline is not None:
        try:
            trusted_baseline.cleanup()
        except BaseException as exc:
            record("trusted baseline cleanup", exc)
    try:
        evidence_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil_module.copytree(openhands_dir, evidence_dir)
    except BaseException as exc:
        record("OpenHands evidence copy", exc)
    try:
        shutil_module.rmtree(openhands_dir, ignore_errors=True)
    except BaseException as exc:
        record("OpenHands temporary directory cleanup", exc)

    preserve = False
    try:
        preserve = bool(
            pending_required
            and pending_path is None
            and gp.output_staging_requires_container_preservation(
                run_dir, cid=cid, name=name
            )
        )
    except BaseException as exc:
        record("container preservation check", exc)
    if preserve:
        metrics["container_preservation_required"] = True
    else:
        completed = False
        try:
            completed = (
                generation_error is None
                and not failures
                and gp.metrics_have_completed_identity(metrics, patch)
            )
        except BaseException as exc:
            record("container completion check", exc)
        try:
            gp.finalize_container_ownership(
                run_dir=run_dir,
                cid=cid,
                name=name,
                keep_container=keep_container if generation_error is None else False,
                completed=completed,
                metrics=metrics,
            )
        except BaseException as exc:
            record("container ownership finalization", exc)

    if not failures:
        return generation_error
    primary = generation_error or failures[0][0]
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        for _exc, message in failures:
            try:
                add_note(f"OpenHands finalization failed ({message})")
            except BaseException:
                pass
    return primary
