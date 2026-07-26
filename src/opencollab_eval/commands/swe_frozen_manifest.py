"""Validate a frozen SWE task selection against an exact dataset snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "opencollab.swe_frozen_task_manifest.v1"
REPORT_SCHEMA = "opencollab.swe_frozen_task_manifest_validation.v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DATASET_LINE_BYTES = 32 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class FrozenManifestError(ValueError):
    """Raised when the manifest or dataset does not match the frozen selection."""


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenManifestError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FrozenManifestError(f"{label} must contain one JSON object")
    return value


def _read_manifest(path: Path) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes()
    label = str(path.resolve())
    if len(raw) > MAX_MANIFEST_BYTES:
        raise FrozenManifestError("manifest exceeds its byte limit")
    return _decode_json(raw, "manifest"), raw, label


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise FrozenManifestError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the static manifest and return its dataset and ordered tasks."""
    if manifest.get("schema") != SCHEMA:
        raise FrozenManifestError(f"manifest schema must be {SCHEMA}")
    dataset = manifest.get("dataset")
    selection = manifest.get("selection")
    tasks = manifest.get("tasks")
    if not isinstance(dataset, dict) or not isinstance(selection, dict) or not isinstance(tasks, list):
        raise FrozenManifestError("manifest requires dataset, selection, and tasks")
    if dataset.get("index_base") != 1:
        raise FrozenManifestError("dataset index_base must be 1")
    row_count = dataset.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise FrozenManifestError("dataset row_count must be a positive integer")
    _require_sha256(dataset.get("sha256"), "dataset.sha256")
    _require_sha256(selection.get("comparison_markdown_sha256"), "selection.comparison_markdown_sha256")
    _require_sha256(selection.get("machine_report_sha256"), "selection.machine_report_sha256")
    if not tasks:
        raise FrozenManifestError("manifest tasks must not be empty")

    indices: list[int] = []
    instance_ids: set[str] = set()
    for position, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise FrozenManifestError(f"manifest task {position} must be an object")
        index = task.get("index")
        instance_id = task.get("instance_id")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= row_count:
            raise FrozenManifestError(f"manifest task {position} has an invalid index")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise FrozenManifestError(f"manifest task {position} has an invalid instance_id")
        if not isinstance(task.get("g11_resolved"), bool) or not isinstance(task.get("openhands_resolved"), bool):
            raise FrozenManifestError(f"manifest task {index} has invalid source membership")
        if task["g11_resolved"] is not True and task["openhands_resolved"] is not True:
            raise FrozenManifestError(f"manifest task {index} belongs to neither source result")
        if index in indices:
            raise FrozenManifestError(f"manifest repeats index {index}")
        if instance_id in instance_ids:
            raise FrozenManifestError(f"manifest repeats instance_id {instance_id}")
        indices.append(index)
        instance_ids.add(instance_id)
    if indices != sorted(indices):
        raise FrozenManifestError("manifest tasks must be ordered by index")
    return dataset, tasks


def _read_dataset(path: Path) -> tuple[list[str], str]:
    digest = hashlib.sha256()
    instance_ids: list[str] = []
    seen: set[str] = set()
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise FrozenManifestError(f"cannot open dataset {path}") from exc
    with handle:
        for line_number, raw in enumerate(handle, start=1):
            digest.update(raw)
            if len(raw) > MAX_DATASET_LINE_BYTES:
                raise FrozenManifestError(f"dataset line {line_number} exceeds its byte limit")
            if not raw.strip():
                raise FrozenManifestError(f"dataset line {line_number} is empty")
            row = _decode_json(raw, f"dataset line {line_number}")
            instance_id = row.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id.strip():
                raise FrozenManifestError(f"dataset line {line_number} has no instance_id")
            if instance_id in seen:
                raise FrozenManifestError(f"dataset repeats instance_id {instance_id}")
            instance_ids.append(instance_id)
            seen.add(instance_id)
    return instance_ids, digest.hexdigest()


def validate_dataset(
    dataset_path: Path,
    manifest: dict[str, Any],
    *,
    manifest_sha256: str = "",
    manifest_source: str = "",
) -> dict[str, Any]:
    """Prove that every frozen index resolves to the expected dataset record."""
    expected_dataset, tasks = validate_manifest(manifest)
    instance_ids, dataset_sha256 = _read_dataset(dataset_path)
    expected_sha256 = str(expected_dataset["sha256"])
    if dataset_sha256 != expected_sha256:
        raise FrozenManifestError(
            f"dataset SHA-256 mismatch: expected {expected_sha256}, got {dataset_sha256}"
        )
    expected_rows = int(expected_dataset["row_count"])
    if len(instance_ids) != expected_rows:
        raise FrozenManifestError(f"dataset row count mismatch: expected {expected_rows}, got {len(instance_ids)}")
    verified: list[dict[str, Any]] = []
    for task in tasks:
        index = int(task["index"])
        actual_instance_id = instance_ids[index - 1]
        expected_instance_id = str(task["instance_id"])
        if actual_instance_id != expected_instance_id:
            raise FrozenManifestError(
                f"dataset index {index} maps to {actual_instance_id}, expected {expected_instance_id}"
            )
        verified.append(dict(task))
    return {
        "schema": REPORT_SCHEMA,
        "status": "verified",
        "manifest_id": manifest.get("manifest_id"),
        "manifest_source": manifest_source,
        "manifest_sha256": manifest_sha256,
        "dataset_path": str(dataset_path.resolve()),
        "dataset_sha256": dataset_sha256,
        "dataset_rows": len(instance_ids),
        "task_count": len(verified),
        "indices": [task["index"] for task in verified],
        "tasks": verified,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a frozen SWE task manifest before starting a solver")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest, raw, source = _read_manifest(args.manifest)
        report = validate_dataset(
            args.dataset,
            manifest,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            manifest_source=source,
        )
    except (FrozenManifestError, OSError) as exc:
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "rejected", "error": str(exc)}), file=sys.stderr)
        return 2
    if args.json_output is not None:
        _write_report(args.json_output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
