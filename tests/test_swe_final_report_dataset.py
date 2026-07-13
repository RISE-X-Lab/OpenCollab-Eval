from __future__ import annotations

import hashlib
import json

import pytest

from opencollab_eval.commands import swe_final_report_dataset as dataset_module
from opencollab_eval.commands.swe_final_report_dataset import (
    DatasetInputError,
    load_dataset_census,
)


@pytest.fixture(autouse=True)
def _trust_synthetic_dataset(monkeypatch):
    monkeypatch.setattr(
        dataset_module,
        "_trusted_dataset_sha256",
        lambda raw: hashlib.sha256(raw).hexdigest(),
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


def test_dataset_census_rejects_a_noncanonical_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "instances.json"
    path.write_text(
        json.dumps([{"instance_id": "task-a", "FAIL_TO_PASS": ["test-a"], "PASS_TO_PASS": []}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(dataset_module, "_trusted_dataset_sha256", lambda _raw: "0" * 64)

    with pytest.raises(DatasetInputError, match="trusted SWE-bench Pro-Lite 1-100 snapshot"):
        load_dataset_census(path, expected=(1,))


def test_trusted_dataset_hash_matches_the_recorded_snapshot():
    assert dataset_module.TRUSTED_DATASET_SHA256 == (
        "a1d473cb415ec0050eee023f373cdf71183436351216240f3f88c820a200c078"
    )
