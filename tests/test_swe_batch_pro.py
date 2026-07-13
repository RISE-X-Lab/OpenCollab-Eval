from __future__ import annotations

import json

import pytest

from opencollab_eval.benchmarks.swe_batch_pro import load_jsonl_dataset, task_from_row


def _row() -> dict:
    return {
        "instance_id": "owner__repo-secret-commit",
        "repo": "owner/repo",
        "problem_statement": "Fix the documented behavior.",
        "base_commit": "secret-base",
        "dockerhub_tag": "owner.repo-task",
        "FAIL_TO_PASS": ["tests/test_secret.py::test_fix"],
        "PASS_TO_PASS": ["tests/test_public.py::test_existing"],
        "test_patch": "secret test patch",
        "solver_public_metadata": {"language": "Python"},
    }


def test_task_adapter_separates_public_and_sealed_data() -> None:
    task = task_from_row(_row())
    serialized_public = repr(task.public)

    assert task.public.task_id.startswith("solver-")
    assert task.public.repo == "owner/repo"
    assert dict(task.public.metadata) == {"language": "Python"}
    assert task.judge.instance_id == "owner__repo-secret-commit"
    assert task.judge.fail_to_pass == ("tests/test_secret.py::test_fix",)
    for secret in ("secret-base", "secret-commit", "test_secret.py", "secret test patch"):
        assert secret not in serialized_public


def test_public_metadata_rejects_sealed_fields() -> None:
    row = _row()
    row["solver_public_metadata"] = {"reference_patch": "answer"}
    with pytest.raises(ValueError, match="sealed field"):
        task_from_row(row)


def test_public_metadata_rejects_sealed_values_under_innocent_keys() -> None:
    row = _row()
    row["solver_public_metadata"] = {"note": "Use secret-base as the starting point"}
    with pytest.raises(ValueError, match="sealed task information"):
        task_from_row(row)


def test_dataset_loader_rejects_symlinks_and_reads_objects(tmp_path) -> None:
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    assert load_jsonl_dataset(dataset) == [_row()]

    symlink = tmp_path / "tasks-link.jsonl"
    symlink.symlink_to(dataset)
    with pytest.raises(OSError):
        load_jsonl_dataset(symlink)
