from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from opencollab_eval.commands import swe_frozen_manifest as frozen


def _dataset_bytes(instance_ids: list[str]) -> bytes:
    return b"".join(
        json.dumps({"instance_id": instance_id}, separators=(",", ":")).encode() + b"\n"
        for instance_id in instance_ids
    )


def _manifest(instance_ids: list[str]) -> dict:
    raw = _dataset_bytes(instance_ids)
    return {
        "schema": frozen.SCHEMA,
        "manifest_id": "test-selection",
        "dataset": {
            "name": "test",
            "index_base": 1,
            "row_count": len(instance_ids),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "selection": {
            "rule": "test",
            "comparison_markdown_sha256": "1" * 64,
            "machine_report_sha256": "2" * 64,
        },
        "tasks": [
            {
                "index": 1,
                "instance_id": instance_ids[0],
                "g11_resolved": True,
                "openhands_resolved": False,
            },
            {
                "index": len(instance_ids),
                "instance_id": instance_ids[-1],
                "g11_resolved": False,
                "openhands_resolved": True,
            },
        ],
    }


def _write_dataset(path: Path, instance_ids: list[str]) -> None:
    path.write_bytes(_dataset_bytes(instance_ids))


def test_cli_requires_an_operator_supplied_manifest() -> None:
    with pytest.raises(SystemExit) as captured:
        frozen.build_parser().parse_args(["--dataset", "tasks.jsonl"])

    assert captured.value.code == 2


def test_exact_dataset_snapshot_and_index_mapping_are_verified(tmp_path: Path) -> None:
    instance_ids = ["task-a", "task-b", "task-c"]
    dataset_path = tmp_path / "instances.jsonl"
    _write_dataset(dataset_path, instance_ids)

    report = frozen.validate_dataset(dataset_path, _manifest(instance_ids))

    assert report["status"] == "verified"
    assert report["dataset_rows"] == 3
    assert report["indices"] == [1, 3]
    assert [task["instance_id"] for task in report["tasks"]] == ["task-a", "task-c"]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("dataset_sha", "dataset SHA-256 mismatch"),
        ("index_mapping", "dataset index 3 maps to task-b, expected task-c"),
        ("source_membership", "belongs to neither source result"),
        ("source_membership_type", "invalid source membership"),
        ("duplicate_index", "repeats index 1"),
    ],
)
def test_invalid_snapshot_or_manifest_is_rejected(
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    instance_ids = ["task-a", "task-b", "task-c"]
    dataset_path = tmp_path / "instances.jsonl"
    manifest = _manifest(instance_ids)
    if case == "dataset_sha":
        _write_dataset(dataset_path, ["task-a", "changed", "task-c"])
    elif case == "index_mapping":
        reordered = ["task-a", "task-c", "task-b"]
        _write_dataset(dataset_path, reordered)
        manifest["dataset"]["sha256"] = hashlib.sha256(_dataset_bytes(reordered)).hexdigest()
    else:
        _write_dataset(dataset_path, instance_ids)
        if case == "source_membership":
            manifest["tasks"][0]["g11_resolved"] = False
        elif case == "source_membership_type":
            manifest["tasks"][0]["g11_resolved"] = 1
        elif case == "duplicate_index":
            manifest["tasks"][1]["index"] = 1

    with pytest.raises(frozen.FrozenManifestError, match=expected):
        frozen.validate_dataset(dataset_path, manifest)


def test_cli_returns_nonzero_before_writing_a_report_for_dataset_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance_ids = ["task-a", "task-b", "task-c"]
    dataset_path = tmp_path / "instances.jsonl"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "validation.json"
    _write_dataset(dataset_path, ["task-a", "changed", "task-c"])
    manifest_path.write_text(json.dumps(_manifest(instance_ids)), encoding="utf-8")

    status = frozen.main(
        [
            "--dataset",
            str(dataset_path),
            "--manifest",
            str(manifest_path),
            "--json-output",
            str(output_path),
        ]
    )

    assert status == 2
    assert not output_path.exists()
    assert json.loads(capsys.readouterr().err)["status"] == "rejected"


def test_manifest_validation_does_not_mutate_the_input() -> None:
    manifest = _manifest(["task-a", "task-b"])
    before = copy.deepcopy(manifest)

    frozen.validate_manifest(manifest)

    assert manifest == before
