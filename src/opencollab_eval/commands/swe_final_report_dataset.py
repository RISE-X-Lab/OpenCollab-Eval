"""Trusted dataset census used by the terminal comparison report."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_report_io as report_io
from opencollab_eval.engine.swe_v1_remote_test_plan import prolite_test_plan

TRUSTED_DATASET_SHA256 = "a1d473cb415ec0050eee023f373cdf71183436351216240f3f88c820a200c078"


class DatasetInputError(ValueError):
    """Raised when a dataset cannot establish the expected task census."""


@dataclass(frozen=True, slots=True)
class DatasetTask:
    index: int
    task: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    fail_to_pass_plan: dict[str, Any]
    pass_to_pass_plan: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    path: Path
    sha256: str
    tasks: tuple[DatasetTask, ...]


def _trusted_dataset_sha256(_raw: bytes) -> str:
    return TRUSTED_DATASET_SHA256


def _dataset_rows(raw: bytes, *, path: Path) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetInputError(f"dataset is not UTF-8: {path}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetInputError(
                    f"dataset JSONL row {line_number} is invalid: {path}"
                ) from exc
            if not isinstance(row, dict):
                raise DatasetInputError(
                    f"dataset JSONL row {line_number} must be an object: {path}"
                ) from None
            rows.append(row)
        return rows
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict) and isinstance(value.get("instances"), list):
        rows = value["instances"]
    else:
        raise DatasetInputError(f"dataset root must be a list, JSONL, or instances object: {path}")
    if not all(isinstance(row, dict) for row in rows):
        raise DatasetInputError(f"every dataset row must be an object: {path}")
    return rows


def _targets(row: dict[str, Any], *keys: str, label: str) -> tuple[str, ...]:
    value = next((row[key] for key in keys if key in row), None)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DatasetInputError(f"dataset {label} must be a JSON list") from exc
    if not isinstance(value, list):
        raise DatasetInputError(f"dataset {label} must be a list")
    targets: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DatasetInputError(f"dataset {label} contains an invalid target")
        target = item.strip()
        if target in targets:
            raise DatasetInputError(f"dataset {label} contains a duplicate target: {target}")
        targets.append(target)
    return tuple(targets)


def load_dataset_census(path: Path, *, expected: tuple[int, ...]) -> LoadedDataset:
    """Read the immutable task order and target tests from one bounded dataset file."""

    try:
        raw = report_io.read_bytes(path, max_bytes=report_io.MAX_REPORT_BYTES)
    except FileNotFoundError as exc:
        raise DatasetInputError(f"dataset is missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise DatasetInputError(f"dataset is unsafe or unstable: {path}") from exc
    if not raw:
        raise DatasetInputError(f"dataset is empty: {path}")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw_sha256 != _trusted_dataset_sha256(raw):
        raise DatasetInputError("dataset does not match the trusted SWE-bench Pro-Lite 1-100 snapshot")
    rows = _dataset_rows(raw, path=path)
    if len(rows) != len(expected):
        raise DatasetInputError(
            f"dataset must contain exactly {len(expected)} ordered task rows"
        )
    tasks: list[DatasetTask] = []
    seen: set[str] = set()
    for position, (index, row) in enumerate(zip(expected, rows, strict=True), start=1):
        task = row.get("instance_id") or row.get("task_id") or row.get("id")
        if not isinstance(task, str) or not task.strip():
            raise DatasetInputError(f"dataset row {position} lacks a stable task identity")
        task = task.strip()
        if task in seen:
            raise DatasetInputError(f"dataset task identity is duplicated: {task}")
        seen.add(task)
        fail_to_pass = _targets(
            row,
            "FAIL_TO_PASS",
            "fail_to_pass",
            label=f"row {position} FAIL_TO_PASS",
        )
        pass_to_pass = _targets(
            row,
            "PASS_TO_PASS",
            "pass_to_pass",
            label=f"row {position} PASS_TO_PASS",
        )
        tasks.append(
            DatasetTask(
                index=index,
                task=task,
                fail_to_pass=fail_to_pass,
                pass_to_pass=pass_to_pass,
                fail_to_pass_plan=prolite_test_plan(
                    row,
                    list(fail_to_pass),
                    target_file="/eval_input/f2p.targets.json",
                ),
                pass_to_pass_plan=prolite_test_plan(
                    row,
                    list(pass_to_pass),
                    target_file="/eval_input/p2p.targets.json",
                ),
            )
        )
    return LoadedDataset(
        path=path,
        sha256=raw_sha256,
        tasks=tuple(tasks),
    )


__all__ = [
    "TRUSTED_DATASET_SHA256",
    "DatasetInputError",
    "DatasetTask",
    "LoadedDataset",
    "load_dataset_census",
]
