from __future__ import annotations

import json

import pytest

from opencollab_eval.commands.swe_final_report_dataset import (
    DatasetInputError,
    load_dataset_census,
)


def test_dataset_census_reads_jsonl_and_literal_target_lists(tmp_path):
    path = tmp_path / "instances.jsonl"
    rows = [
        {
            "instance_id": "task-a",
            "FAIL_TO_PASS": json.dumps(["tests/test_a.py::test_a"]),
            "PASS_TO_PASS": json.dumps([]),
        },
        {
            "instance_id": "task-b",
            "fail_to_pass": ["pkg/widget_test.go::TestWidget"],
            "pass_to_pass": ["pkg/widget_test.go::TestStable"],
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    dataset = load_dataset_census(path, expected=(1, 2))

    assert [task.task for task in dataset.tasks] == ["task-a", "task-b"]
    assert dataset.tasks[0].fail_to_pass == ("tests/test_a.py::test_a",)
    assert dataset.tasks[1].pass_to_pass == ("pkg/widget_test.go::TestStable",)
    assert len(dataset.sha256) == 64


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"instance_id": "task-a", "FAIL_TO_PASS": [], "PASS_TO_PASS": []}], "exactly 2"),
        (
            [
                {"instance_id": "task-a", "FAIL_TO_PASS": ["test-a"], "PASS_TO_PASS": []},
                {"instance_id": "task-a", "FAIL_TO_PASS": ["test-b"], "PASS_TO_PASS": []},
            ],
            "duplicated",
        ),
        (
            [
                {"instance_id": "task-a", "FAIL_TO_PASS": ["test-a", "test-a"], "PASS_TO_PASS": []},
                {"instance_id": "task-b", "FAIL_TO_PASS": ["test-b"], "PASS_TO_PASS": []},
            ],
            "duplicate target",
        ),
    ],
)
def test_dataset_census_rejects_incomplete_or_ambiguous_rows(tmp_path, rows, message):
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(DatasetInputError, match=message):
        load_dataset_census(path, expected=(1, 2))


def test_dataset_census_rejects_a_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps([{"instance_id": "task-a", "FAIL_TO_PASS": ["test-a"], "PASS_TO_PASS": []}]),
        encoding="utf-8",
    )
    path = tmp_path / "instances.json"
    path.symlink_to(target)

    with pytest.raises(DatasetInputError, match="unsafe or unstable"):
        load_dataset_census(path, expected=(1,))
