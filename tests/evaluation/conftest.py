"""Factories for terminal rejudge reports and reliability plans."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.commands import swe_rejudge_queue as queue


@pytest.fixture
def accept_rejudge_terminal():
    def prepare(monkeypatch) -> None:
        monkeypatch.setattr(
            queue._swe_eval_layer_integrity,
            "attempt_integrity",
            lambda row, task: SimpleNamespace(
                direct_execution_proven=True,
                reasons=(),
            ),
        )

    return prepare


@pytest.fixture
def write_rejudge_terminal_report():
    def prepare(
        path: Path,
        *,
        index: int,
        patch_sha256: str,
        resolved: bool,
        summary_status: str = "eval_done",
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "index": index,
                            "task": "instance_owner__repo-25",
                            "generation": {
                                "record_id": "record-25",
                                "patch_sha256": patch_sha256,
                                "source_patch_sha256": patch_sha256,
                                "eval_patch_sha256": patch_sha256,
                            },
                            "eval": {
                                "status": "eval_done",
                                "summary": {"status": summary_status, "resolved": resolved},
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    return prepare


@pytest.fixture
def rejudge_reliability_plan():
    def prepare(tmp_path: Path, *, patch_sha256: str = "a" * 64) -> tuple[Path, Path]:
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "parallel_summary.json").write_text("{}", encoding="utf-8")
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "schema": queue.SCHEMA,
                    "runner_args": ["--host", "worker"],
                    "jobs": [
                        {
                            "index": 25,
                            "parent_output_dir": str(parent),
                            "base_run_dir": "/worker/run/task_25",
                            "remote_runtime_repo": "/worker/runtime/task_25",
                            "run_id": "rejudge-task-25",
                            "eval_dir_name": "official_eval_rejudge",
                            "task": "instance_owner__repo-25",
                            "record_id": "record-25",
                            "source_patch_sha256": patch_sha256,
                            "eval_patch_sha256": patch_sha256,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return plan, parent

    return prepare
