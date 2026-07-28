from __future__ import annotations

import json

import pytest

from opencollab_eval.benchmarks.swe_batch_pro import (
    load_identity_key,
    load_jsonl_dataset,
    task_from_row,
    tasks_from_rows,
)

_IDENTITY_KEY = b"test identity key".ljust(32, b"-")
_IMAGE_REPOSITORY = "registry.example.invalid/swe-images"


def _row() -> dict:
    return {
        "instance_id": "owner__repo-secret-commit",
        "repo": "owner/repo",
        "problem_statement": "Fix the documented behavior.",
        "requirements": "Preserve documented compatibility.",
        "interface": "fix(value: str) -> str",
        "base_commit": "secret-base",
        "dockerhub_tag": "owner.repo-task",
        "FAIL_TO_PASS": ["tests/test_secret.py::test_fix"],
        "PASS_TO_PASS": ["tests/test_public.py::test_existing"],
        "test_patch": "secret test patch",
        "solver_public_metadata": {"language": "Python"},
    }


def test_task_adapter_separates_public_and_sealed_data() -> None:
    task = task_from_row(
        _row(),
        identity_key=_IDENTITY_KEY,
        image_repository=_IMAGE_REPOSITORY,
    )
    serialized_public = repr(task.public)

    assert task.public.task_id.startswith("solver-")
    assert task.public.repo == "owner/repo"
    assert (
        "Requirements:\nPreserve documented compatibility."
        in task.public.problem_statement
    )
    assert (
        "New interfaces introduced:\nfix(value: str) -> str"
        in task.public.problem_statement
    )
    assert dict(task.public.metadata) == {"language": "Python"}
    assert task.judge.instance_id == "owner__repo-secret-commit"
    assert task.judge.fail_to_pass == ("tests/test_secret.py::test_fix",)
    for secret in ("secret-base", "secret-commit", "test_secret.py", "secret test patch"):
        assert secret not in serialized_public


def test_public_metadata_rejects_sealed_fields() -> None:
    row = _row()
    row["solver_public_metadata"] = {"reference_patch": "answer"}
    with pytest.raises(ValueError, match="sealed field"):
        task_from_row(
            row,
            identity_key=_IDENTITY_KEY,
            image_repository=_IMAGE_REPOSITORY,
        )


def test_public_metadata_rejects_sealed_values_under_innocent_keys() -> None:
    row = _row()
    row["solver_public_metadata"] = {"note": "Use secret-base as the starting point"}
    with pytest.raises(ValueError, match="sealed task information"):
        task_from_row(
            row,
            identity_key=_IDENTITY_KEY,
            image_repository=_IMAGE_REPOSITORY,
        )


def test_public_metadata_is_deeply_immutable() -> None:
    row = _row()
    row["solver_public_metadata"] = {
        "safe": {"language": "Python"},
        "items": [{"label": "public"}],
    }
    task = task_from_row(
        row,
        identity_key=_IDENTITY_KEY,
        image_repository=_IMAGE_REPOSITORY,
    )

    with pytest.raises(TypeError):
        task.public.metadata["safe"]["base_commit"] = "LEAK"
    with pytest.raises(TypeError):
        task.public.metadata["items"][0]["test_patch"] = "LEAK"
    with pytest.raises(AttributeError):
        task.public.metadata["items"].append("LEAK")


def test_public_metadata_rejects_non_finite_numbers() -> None:
    row = _row()
    row["solver_public_metadata"] = {"score": float("nan")}
    with pytest.raises(ValueError, match="finite"):
        task_from_row(
            row,
            identity_key=_IDENTITY_KEY,
            image_repository=_IMAGE_REPOSITORY,
        )


def test_public_identity_binds_each_judge_spec_and_batch_rejects_duplicates() -> None:
    first = _row()
    second = _row()
    second["instance_id"] = "owner__repo-another-secret-commit"

    tasks = tasks_from_rows(
        (first, second),
        identity_key=_IDENTITY_KEY,
        image_repository=_IMAGE_REPOSITORY,
    )
    assert tasks[0].public.task_id != tasks[1].public.task_id
    with pytest.raises(ValueError, match="duplicate public identity"):
        tasks_from_rows(
            (first, first),
            identity_key=_IDENTITY_KEY,
            image_repository=_IMAGE_REPOSITORY,
        )


def test_public_identity_requires_an_evaluator_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        task_from_row(
            _row(),
            identity_key=b"short",
            image_repository=_IMAGE_REPOSITORY,
        )


def test_task_adapter_requires_explicit_image_source() -> None:
    with pytest.raises(ValueError, match="docker_image or image_repository"):
        task_from_row(_row(), identity_key=_IDENTITY_KEY)


def test_dataset_loader_rejects_symlinks_and_reads_objects(tmp_path) -> None:
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    assert load_jsonl_dataset(dataset) == [_row()]

    symlink = tmp_path / "tasks-link.jsonl"
    symlink.symlink_to(dataset)
    with pytest.raises(OSError):
        load_jsonl_dataset(symlink)


def test_identity_key_loader_requires_raw_regular_32_byte_file(tmp_path) -> None:
    key_path = tmp_path / "identity.key"
    key_path.write_bytes(_IDENTITY_KEY)
    assert load_identity_key(key_path) == _IDENTITY_KEY

    key_path.write_bytes(b"short")
    with pytest.raises(ValueError, match="exactly 32"):
        load_identity_key(key_path)

    key_path.write_bytes(_IDENTITY_KEY)
    symlink = tmp_path / "identity-link.key"
    symlink.symlink_to(key_path)
    with pytest.raises(OSError):
        load_identity_key(symlink)
