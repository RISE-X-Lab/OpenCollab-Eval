"""Read cell metrics in persisted prediction attempt order after interrupted appends."""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

from opencollab_eval.candidate_bytes import encode_candidate_jsonl, validate_candidate_read
from opencollab_eval.engine.swe_eval_records import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_RETAINED_BYTES,
    MAX_JSONL_RETAINED_ROWS,
    MAX_JSONL_SCAN_BYTES,
    RecordInputFormatError,
    RecordInputLimitError,
    UnsafeRecordInputError,
    embedded_workflow_metric,
    latest_paired_rows,
    open_regular_binary,
    read_jsonl,
    row_task_id,
)


def report_metric_records(cell: Path, metrics: Path, arm: str) -> list[dict[str, Any]]:
    """Place matched metrics in prediction attempt order after metrics-only history.

    A recovered metric can be appended after a later attempt's metric. The
    predictions file retains the attempt order, while metric append order then
    describes recovery timing. Older batches with metrics alone retain their
    original order. An embedded workflow metric fills a missing sidecar row
    only when the evaluation pairing accepts its task, record and patch identity.
    """
    try:
        recorded = read_jsonl(metrics)
        damaged_lines: list[tuple[int, bytes]] = []
    except RecordInputFormatError:
        recorded, damaged_lines = _read_metrics_with_damaged_lines(metrics)
    predictions_path = cell / f"preds-{arm}.jsonl"
    if not predictions_path.exists():
        predictions_path = cell / "predictions.jsonl"
    if not predictions_path.exists():
        if damaged_lines:
            raise RecordInputFormatError(f"damaged metric rows without prediction recovery source: {metrics}")
        return recorded
    predictions = read_jsonl(predictions_path)
    if damaged_lines:
        encoded_metrics = [
            encode_candidate_jsonl(metric)
            for prediction in predictions
            if (metric := embedded_workflow_metric(prediction)) is not None
        ]
        for line_number, line in damaged_lines:
            prefix = line.removesuffix(b"\n").removesuffix(b"\r")
            if not prefix or not any(
                len(prefix) < len(encoded) and encoded.startswith(prefix) for encoded in encoded_metrics
            ):
                raise RecordInputFormatError(
                    f"damaged metric row at {metrics}:{line_number} has no matching prediction prefix"
                )
        warnings.warn(
            f"{metrics} has damaged JSONL lines {[number for number, _line in damaged_lines]}; "
            "valid rows and prediction-embedded metrics remain reportable",
            RuntimeWarning,
            stacklevel=2,
        )

    by_task: dict[str, list[dict[str, Any]]] = {}
    for metric in recorded:
        by_task.setdefault(row_task_id(metric), []).append(metric)
    ordered: list[dict[str, Any]] = []
    used: set[int] = set()
    for prediction in predictions:
        task_id = row_task_id(prediction)
        if not task_id:
            continue
        pair = latest_paired_rows([prediction], by_task.get(task_id, []), task_id)
        if pair.metric is None or pair.status not in {"record_id", "patch_sha", "embedded_metric"}:
            continue
        if pair.status == "embedded_metric":
            ordered.append(pair.metric)
        elif id(pair.metric) not in used:
            used.add(id(pair.metric))
            ordered.append(pair.metric)
    return [metric for metric in recorded if id(metric) not in used] + ordered


def _read_metrics_with_damaged_lines(path: Path) -> tuple[list[dict[str, Any]], list[tuple[int, bytes]]]:
    """Retain complete metric rows around a torn append under the JSONL input limits."""
    rows: list[dict[str, Any]] = []
    malformed: list[tuple[int, bytes]] = []
    retained_bytes = 0
    with open_regular_binary(path) as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > MAX_JSONL_SCAN_BYTES:
            raise RecordInputLimitError(f"JSONL input exceeds {MAX_JSONL_SCAN_BYTES} bytes: {path}")
        remaining = before.st_size
        line_number = 0
        while remaining > 0:
            line = handle.readline(min(MAX_JSONL_LINE_BYTES + 1, remaining))
            if not line:
                break
            remaining -= len(line)
            line_number += 1
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise RecordInputLimitError(f"JSONL line exceeds {MAX_JSONL_LINE_BYTES} bytes: {path}")
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed.append((line_number, line))
                continue
            if not isinstance(row, dict):
                malformed.append((line_number, line))
                continue
            validate_candidate_read(row, RecordInputLimitError)
            rows.append(row)
            retained_bytes += len(line)
            if len(rows) > MAX_JSONL_RETAINED_ROWS or retained_bytes > MAX_JSONL_RETAINED_BYTES:
                raise RecordInputLimitError(f"JSONL input exceeds retained row or byte limit: {path}")
        after = os.fstat(handle.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise UnsafeRecordInputError(f"JSONL input changed while reading: {path}")
    return rows, malformed
