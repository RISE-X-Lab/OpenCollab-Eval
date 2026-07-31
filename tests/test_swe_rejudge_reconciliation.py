from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import swe_rejudge_direct_eval as rejudge


def _write(path: Path, value: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return raw


def test_reconciliation_binds_the_derived_verdict_to_one_launcher_row(
    tmp_path,
    monkeypatch,
):
    task = "task-1"
    source_sha = "a" * 64
    eval_sha = "b" * 64
    eval_spec_sha = "c" * 64
    eval_image_id = f"sha256:{'d' * 64}"
    source = {"status": "technical_eval_failed", "task": task}
    derived_dir = tmp_path / "derived"
    source_raw = _write(derived_dir / "source_summary.json", source)
    derived = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "task": task,
        "resolved": False,
        "record_id": "record-1",
        "source_patch_sha256": source_sha,
        "eval_patch_sha256": eval_sha,
        "eval_spec_sha256": eval_spec_sha,
        "eval_image_id": eval_image_id,
        "rejudgement": {
            "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
            "source_summary_sha256": hashlib.sha256(source_raw).hexdigest(),
            "matching_eval_attempts": 2,
            "added_eval_attempts": 0,
            "attempt_identity": {
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
                "eval_patch_sha256": eval_sha,
                "eval_spec_sha256": eval_spec_sha,
                "eval_image_id": eval_image_id,
            },
        },
    }
    _write(derived_dir / "summary.json", derived)
    _write(derived_dir / "report.json", {task: derived})
    launcher = {
        "status": "technical_eval_failed",
        "rows": [
            {
                "index": 31,
                "task": task,
                "generation": {
                    "status": "generation_done",
                    "record_id": "record-1",
                    "source_patch_sha256": source_sha,
                    "eval_patch_sha256": eval_sha,
                },
                "eval": {
                    "status": "technical_eval_failed",
                    "summary": source,
                    "attempt_count": 2,
                },
            }
        ],
    }
    launcher_path = tmp_path / "launcher.json"
    _write(launcher_path, launcher)
    monkeypatch.setattr(
        rejudge,
        "direct_eval_done_has_execution_proof",
        lambda payload: payload == derived,
    )
    monkeypatch.setattr(
        rejudge._swe_eval_layer_integrity,
        "attempt_integrity",
        lambda row, candidate_task: SimpleNamespace(
            reasons=(),
            direct_execution_proven=(
                candidate_task == task
                and row["eval"]["summary"] == derived
                and row["eval"]["executed"] is False
            ),
        ),
    )
    output = tmp_path / "task_31_eval_only_rejudged.json"

    reconciled = rejudge.reconcile_launcher_report(
        launcher_path,
        derived_dir,
        output,
    )

    persisted = json.loads(output.read_text())
    assert reconciled == persisted
    assert persisted["status"] == "done"
    assert persisted["rows"][0]["eval"]["status"] == "eval_done"
    assert persisted["rows"][0]["eval"]["attempt_count"] == 2
    assert persisted["rows"][0]["eval"]["summary"]["resolved"] is False
    assert persisted["rejudgement"]["launcher_report_sha256"] == hashlib.sha256(
        launcher_path.read_bytes()
    ).hexdigest()


def test_reconciliation_source_change_does_not_publish_or_block_retry(
    tmp_path,
    monkeypatch,
):
    task = "task-1"
    source_sha = "a" * 64
    eval_sha = "b" * 64
    eval_spec_sha = "c" * 64
    eval_image_id = f"sha256:{'d' * 64}"
    source = {"status": "technical_eval_failed", "task": task}
    derived_dir = tmp_path / "derived"
    source_raw = _write(derived_dir / "source_summary.json", source)
    derived = {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "task": task,
        "resolved": False,
        "record_id": "record-1",
        "source_patch_sha256": source_sha,
        "eval_patch_sha256": eval_sha,
        "eval_spec_sha256": eval_spec_sha,
        "eval_image_id": eval_image_id,
        "rejudgement": {
            "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
            "source_summary_sha256": hashlib.sha256(source_raw).hexdigest(),
            "matching_eval_attempts": 2,
            "added_eval_attempts": 0,
            "attempt_identity": {
                "task": task,
                "record_id": "record-1",
                "patch_sha256": source_sha,
                "eval_patch_sha256": eval_sha,
                "eval_spec_sha256": eval_spec_sha,
                "eval_image_id": eval_image_id,
            },
        },
    }
    _write(derived_dir / "summary.json", derived)
    _write(derived_dir / "report.json", {task: derived})
    launcher = {
        "rows": [
            {
                "index": 31,
                "task": task,
                "generation": {
                    "record_id": "record-1",
                    "source_patch_sha256": source_sha,
                    "eval_patch_sha256": eval_sha,
                },
                "eval": {"summary": source, "attempt_count": 2},
            }
        ],
    }
    launcher_path = tmp_path / "launcher.json"
    original_launcher_raw = _write(launcher_path, launcher)
    monkeypatch.setattr(rejudge, "direct_eval_done_has_execution_proof", lambda payload: True)
    monkeypatch.setattr(
        rejudge._swe_eval_layer_integrity,
        "attempt_integrity",
        lambda row, candidate_task: SimpleNamespace(
            reasons=(),
            direct_execution_proven=True,
        ),
    )
    real_read = rejudge._read_regular_bytes
    launcher_reads = 0

    def changing_read(path, *, limit):
        nonlocal launcher_reads
        data = real_read(path, limit=limit)
        if path == launcher_path:
            launcher_reads += 1
            if launcher_reads == 2:
                launcher_path.write_bytes(original_launcher_raw + b" ")
                return launcher_path.read_bytes()
        return data

    monkeypatch.setattr(rejudge, "_read_regular_bytes", changing_read)
    output = tmp_path / "reconciled.json"

    with pytest.raises(RuntimeError, match="source artifact changed"):
        rejudge.reconcile_launcher_report(launcher_path, derived_dir, output)

    assert not output.exists()
    launcher_path.write_bytes(original_launcher_raw)
    monkeypatch.setattr(rejudge, "_read_regular_bytes", real_read)
    rejudge.reconcile_launcher_report(launcher_path, derived_dir, output)
    assert output.is_file()
