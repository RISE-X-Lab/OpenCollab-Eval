"""The analyst-boundary extraction has to be exact, and has to refuse."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_patch_preds import build, same_patch, snapshot_body  # noqa: E402

PATCH = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n"
    "index 996d2e4..66125b4 100644\n"
    "--- a/pkg/mod.py\n"
    "+++ b/pkg/mod.py\n"
    "@@ -1,2 +1,3 @@\n"
    " a\n"
    "+b\n"
    " c\n"
)
SNAPSHOT = "[Working tree status]\n M pkg/mod.py\n\n[Tracked changes vs HEAD]\n" + PATCH


def write(tmp_path: Path, runs: list[dict], graded: dict[str, str]) -> Path:
    batch = tmp_path / "b"
    batch.mkdir()
    (batch / "metrics.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in runs), encoding="utf-8"
    )
    (batch / "preds-arm.jsonl").write_text(
        "".join(
            json.dumps({"instance_id": i, "model_patch": p}) + "\n"
            for i, p in graded.items()
        ),
        encoding="utf-8",
    )
    return batch


def run(instance: str, diff: str | None, truncated: bool = False) -> dict:
    return {
        "instance_id": instance,
        "llm_model": "m",
        "workflow_result": {
            "tree_snapshots": [
                {"after": "analyze", "diff": diff, "truncated": truncated},
                {"after": "implement:r1", "diff": diff},
            ]
        },
    }


def test_body_is_the_text_after_the_marker() -> None:
    assert snapshot_body(SNAPSHOT) == PATCH
    assert snapshot_body("[Working tree status]\n(clean)") == ""
    assert snapshot_body(None) == ""


def test_only_the_abbreviated_index_sha_is_normalised() -> None:
    full = PATCH.replace("index 996d2e4..66125b4", "index 996d2e4c43ea..66125b437623")
    assert same_patch(PATCH, full)
    assert not same_patch(PATCH, PATCH.replace("+b\n", "+bb\n"))


def test_identical_rows_are_counted_as_the_scoring_control(tmp_path: Path) -> None:
    batch = write(tmp_path, [run("i1", SNAPSHOT)], {"i1": PATCH})
    rows, side = build(batch, "arm")
    assert [r["model_patch"] for r in rows] == [PATCH]
    assert side["identical_to_graded"] == 1
    assert side["rows_with_a_patch"] == 1


def test_a_null_probe_is_refused_not_written_as_an_empty_patch(tmp_path: Path) -> None:
    """The failure this refusal exists for: null read as "" agrees with an empty
    submission, and a failed measurement becomes evidence of agreement."""
    batch = write(tmp_path, [run("i1", None)], {"i1": ""})
    rows, side = build(batch, "arm")
    assert rows == []
    assert side["refused"] == [{"instance_id": "i1", "why": "snapshot probe returned null"}]
    assert side["identical_to_graded"] == 0


def test_a_truncated_snapshot_is_refused(tmp_path: Path) -> None:
    batch = write(tmp_path, [run("i1", SNAPSHOT, truncated=True)], {"i1": PATCH})
    rows, side = build(batch, "arm")
    assert rows == []
    assert side["refused"][0]["why"] == "snapshot truncated at the cap"


def test_an_empty_metrics_file_is_an_error(tmp_path: Path) -> None:
    batch = write(tmp_path, [], {})
    with pytest.raises(SystemExit):
        build(batch, "arm")
