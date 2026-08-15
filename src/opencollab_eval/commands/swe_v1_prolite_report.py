"""Atomic local report publication for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_eval_layer_integrity as _integrity
from opencollab_eval.commands import _swe_report_io
from opencollab_eval.safe_files import write_regular_bytes_atomic


def _local_report_target_expectation(path: Path) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"report destination must be regular or absent: {path}")
    if before is None:
        return {"require_target_absent": True}
    return {"expected_target_identity": (before.st_dev, before.st_ino)}


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    if os.path.abspath(json_path) == os.path.abspath(md_path):
        raise ValueError("JSON and Markdown reports must use different paths")
    bundle_id = uuid.uuid4().hex
    bundled_summary = {**summary, "local_report_bundle_id": bundle_id}
    json_payload = (json.dumps(bundled_summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    markdown = markdown.rstrip("\n") + f"\n\n<!-- local_report_bundle_id:{bundle_id} -->\n"
    json_expectation = _local_report_target_expectation(json_path)
    md_expectation = _local_report_target_expectation(md_path)
    # JSON is the bundle commit marker. A reader accepts the pair only when its
    # bundle id matches the Markdown marker, so publish Markdown first.
    write_regular_bytes_atomic(
        md_path,
        markdown.encode("utf-8"),
        **md_expectation,
    )
    write_regular_bytes_atomic(json_path, json_payload, **json_expectation)


def eval_only_reconciliation_reports(
    parent_output_dir: Path,
    current_report: Path,
) -> list[Path]:
    """Select cumulative execution evidence and the newest verdict per task."""
    candidates = set(parent_output_dir.glob("task_*_eval_only_*.json"))
    final_report_path = parent_output_dir / "final_eval_layer_report.json"
    try:
        final_report_path.lstat()
    except FileNotFoundError:
        pass
    else:
        final_report, load_error = _swe_report_io.load_json_with_error(
            final_report_path
        )
        if load_error:
            raise RuntimeError(
                f"prior final report is unavailable: {load_error}: {final_report_path}"
            )
        source_reports = final_report.get("source_reports")
        if not isinstance(source_reports, list) or not source_reports or any(
            not isinstance(value, str) or not value or not Path(value).is_absolute()
            for value in source_reports
        ):
            raise RuntimeError(
                f"prior final report has invalid source_reports: {final_report_path}"
            )
        parent_summary = (parent_output_dir / "parallel_summary.json").absolute()
        for value in source_reports:
            historical_path = Path(value).absolute()
            if historical_path != parent_summary:
                candidates.add(historical_path)
    candidates.add(current_report.absolute())
    selected_execution: set[Path] = set()
    selected_verdict: dict[int, tuple[tuple[int, str], Path]] = {}
    for path in candidates:
        path = path.absolute()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"eval-only report must be a regular file: {path}")
        report = _swe_report_io.load_json(path)
        rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
        indices = {_integrity.strict_index(row.get("index")) for row in rows}
        indices.discard(None)
        if len(rows) != 1 or len(indices) != 1:
            raise RuntimeError(f"eval-only report must contain one indexed row: {path}")
        index = next(iter(indices))
        verdict_score = (info.st_mtime_ns, str(path))
        if index not in selected_verdict or verdict_score > selected_verdict[index][0]:
            selected_verdict[index] = (verdict_score, path)
        executed = _integrity.eval_attempt_count(rows[0])
        if executed:
            selected_execution.add(path)
    selected_paths = selected_execution | {
        entry[1] for entry in selected_verdict.values()
    }
    return sorted(selected_paths, key=str)


__all__ = [name for name in globals() if not name.startswith("__")]
