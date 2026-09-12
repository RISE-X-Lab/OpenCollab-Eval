"""Hash-bound isolation of one historical candidate for eval-only execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_eval_records import (
    prediction_patch,
    row_record_id,
    row_task_id,
)
from opencollab_eval.patch_diff import filter_model_patch_for_eval
from opencollab_eval.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_bytes,
)

SCHEMA = "opencollab.swe_eval_only_candidate_isolation.v1"
MAX_CANDIDATE_RECORD_BYTES = 256 * 1024 * 1024
MAX_ISOLATION_MANIFEST_BYTES = 1024 * 1024


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _jsonl_rows(payload: bytes, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in payload.splitlines():
        if not line.strip():
            raise ValueError(f"candidate isolation {label} contains a blank row")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"candidate isolation {label} is invalid JSONL") from exc
        if not isinstance(value, dict):
            raise ValueError(  # noqa: TRY004 - all candidate document failures share one contract
                f"candidate isolation {label} row must be an object"
            )
        rows.append(value)
    if not rows:
        raise ValueError(f"candidate isolation {label} is empty")
    return rows


def _bound_rows(
    predictions: bytes,
    metrics: bytes,
    *,
    task: str,
    record_id: str,
    source_patch_sha256: str,
    eval_patch_sha256: str,
) -> None:
    prediction_rows = [
        row
        for row in _jsonl_rows(predictions, label="predictions")
        if row_task_id(row) == task and row_record_id(row) == record_id
    ]
    metric_rows = [
        row
        for row in _jsonl_rows(metrics, label="metrics")
        if row_task_id(row) == task and row_record_id(row) == record_id
    ]
    if not prediction_rows or not metric_rows:
        raise ValueError("candidate isolation record identity is missing")
    patch = prediction_patch(prediction_rows[-1])
    if _sha256(patch.encode("utf-8")) != source_patch_sha256:
        raise ValueError("candidate isolation source patch SHA mismatch")
    filtered = filter_model_patch_for_eval(patch)
    if _sha256(filtered.encode("utf-8")) != eval_patch_sha256:
        raise ValueError("candidate isolation eval patch SHA mismatch")


def _copy_or_verify(path: Path, payload: bytes) -> None:
    try:
        existing = read_regular_bytes(
            path,
            max_bytes=MAX_CANDIDATE_RECORD_BYTES,
        )
    except FileNotFoundError:
        create_regular_bytes_atomic(
            path,
            payload,
            max_bytes=MAX_CANDIDATE_RECORD_BYTES,
        )
        return
    if existing != payload:
        raise RuntimeError(f"isolated candidate artifact identity mismatch: {path}")


def isolate_eval_only_candidate(
    *,
    source_base_run_dir: Path,
    target_base_run_dir: Path,
    task: str,
    record_id: str,
    source_patch_sha256: str,
    eval_patch_sha256: str,
) -> dict[str, Any]:
    """Copy only immutable candidate records into a fresh evaluation base."""
    source_base = Path(source_base_run_dir).absolute()
    target_base = Path(target_base_run_dir).absolute()
    if source_base == target_base:
        raise ValueError("eval-only candidate source and target bases must differ")
    source_task = source_base / task
    target_task = target_base / task
    predictions = read_regular_bytes(
        source_task / "predictions.jsonl",
        max_bytes=MAX_CANDIDATE_RECORD_BYTES,
    )
    metrics = read_regular_bytes(
        source_task / "metrics.jsonl",
        max_bytes=MAX_CANDIDATE_RECORD_BYTES,
    )
    _bound_rows(
        predictions,
        metrics,
        task=task,
        record_id=record_id,
        source_patch_sha256=source_patch_sha256,
        eval_patch_sha256=eval_patch_sha256,
    )
    ensure_directory_no_symlinks(target_task)
    _copy_or_verify(target_task / "predictions.jsonl", predictions)
    _copy_or_verify(target_task / "metrics.jsonl", metrics)
    if (
        read_regular_bytes(
            source_task / "predictions.jsonl",
            max_bytes=MAX_CANDIDATE_RECORD_BYTES,
        )
        != predictions
        or read_regular_bytes(
            source_task / "metrics.jsonl",
            max_bytes=MAX_CANDIDATE_RECORD_BYTES,
        )
        != metrics
    ):
        raise RuntimeError("candidate isolation source changed during copy")
    manifest = {
        "schema": SCHEMA,
        "source_base_run_dir": str(source_base),
        "target_base_run_dir": str(target_base),
        "task": task,
        "record_id": record_id,
        "source_patch_sha256": source_patch_sha256,
        "eval_patch_sha256": eval_patch_sha256,
        "predictions_sha256": _sha256(predictions),
        "metrics_sha256": _sha256(metrics),
    }
    manifest_path = target_task / "eval_only_candidate_isolation.json"
    encoded = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    try:
        observed = read_regular_bytes(
            manifest_path,
            max_bytes=MAX_ISOLATION_MANIFEST_BYTES,
        )
    except FileNotFoundError:
        create_regular_bytes_atomic(
            manifest_path,
            encoded,
            max_bytes=MAX_ISOLATION_MANIFEST_BYTES,
        )
    else:
        if observed != encoded:
            raise RuntimeError("eval-only candidate isolation manifest mismatch")
    return manifest


__all__ = [
    "MAX_CANDIDATE_RECORD_BYTES",
    "MAX_ISOLATION_MANIFEST_BYTES",
    "SCHEMA",
    "isolate_eval_only_candidate",
]
