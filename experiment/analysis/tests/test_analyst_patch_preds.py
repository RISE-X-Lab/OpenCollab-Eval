"""The analyst-boundary extraction has to be exact, and has to refuse."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_patch_preds import build, same_patch, snapshot_body  # noqa: E402

from opencollab_eval.engine.swe_eval_records import patch_sha  # noqa: E402

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
    (batch / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs), encoding="utf-8")
    (batch / "preds-arm.jsonl").write_text(
        "".join(json.dumps({"instance_id": i, "model_patch": p}) + "\n" for i, p in graded.items()),
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


def _attempt(instance: str, record_id: str, patch: str, status: str = "completed") -> tuple[dict, dict]:
    metric = run(instance, "[Tracked changes vs HEAD]\n" + patch)
    metric.update({"record_id": record_id, "patch_sha256": patch_sha(patch), "run_summary": {"status": status}})
    prediction = {
        "instance_id": instance, "record_id": record_id, "model_patch": patch, "patch_sha256": patch_sha(patch),
    }
    return prediction, metric


def _write_attempts(batch: Path, predictions: list[dict], metrics: list[dict]) -> None:
    batch.mkdir(exist_ok=True)
    for name, values in [("preds-arm.jsonl", predictions), ("metrics.jsonl", metrics)]:
        (batch / name).write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def test_recovered_old_metric_does_not_export_an_old_or_duplicate_prediction(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH.replace("+b\n", "+old\n"), "failed")
    new_pred, new_metric = _attempt("case-a", "new", PATCH)
    _write_attempts(tmp_path, [old_pred, new_pred], [new_metric, old_metric])
    rows, side = build(tmp_path, "arm")
    assert [(row["instance_id"], row["record_id"], row["model_patch"]) for row in rows] == [("case-a", "new", PATCH)]
    assert side["identical_to_graded"] == 1
    assert side["rows"] == 1 and side["refused"] == []


def test_last_attempt_that_ran_is_compared_with_its_own_graded_patch(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH)
    new_pred, new_metric = _attempt("case-a", "new", PATCH.replace("+b\n", "+new\n"), "failed")
    _write_attempts(tmp_path, [old_pred, new_pred], [new_metric, old_metric])
    rows, side = build(tmp_path, "arm")
    assert rows[0]["record_id"] == "old" and rows[0]["model_patch"] == PATCH
    assert side["identical_to_graded"] == 1


def test_patch_identity_selects_an_older_valid_attempt_without_record_ids(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH)
    new_pred, new_metric = _attempt("case-a", "new", PATCH.replace("+b\n", "+new\n"), "failed")
    for value in (old_pred, old_metric, new_pred, new_metric):
        del value["record_id"]
    _write_attempts(tmp_path, [old_pred, new_pred], [new_metric, old_metric])
    rows, side = build(tmp_path, "arm")
    assert len(rows) == 1 and rows[0]["model_patch"] == PATCH
    assert side["identical_to_graded"] == 1


def test_metrics_only_history_follows_the_formal_adoption_policy(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH)
    _, new_metric = _attempt("case-a", "new", PATCH.replace("+b\n", "+new\n"))
    _write_attempts(tmp_path, [old_pred], [old_metric, new_metric])
    rows, side = build(tmp_path, "arm")
    # Matched prediction order places the persisted old attempt after metrics-only
    # history, as in the formal cell report.
    assert rows[0]["record_id"] == "old"
    assert side["identical_to_graded"] == 1


def test_missing_prediction_for_selected_attempt_is_refused(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH, "failed")
    _, new_metric = _attempt("case-a", "new", PATCH.replace("+b\n", "+new\n"))
    _write_attempts(tmp_path, [old_pred], [old_metric, new_metric])
    rows, side = build(tmp_path, "arm")
    assert rows == [] and side["identical_to_graded"] == 0
    assert side["refused"][0]["why"] == "selected attempt has no unique paired prediction"


def test_ambiguous_legacy_attempts_are_refused(tmp_path: Path) -> None:
    _write_attempts(
        tmp_path,
        [{"instance_id": "case-a", "model_patch": PATCH}],
        [run("case-a", SNAPSHOT), run("case-a", SNAPSHOT)],
    )
    rows, side = build(tmp_path, "arm")
    assert rows == [] and side["identical_to_graded"] == 0
    assert side["refused"][0]["why"] == "selected attempt has no unique paired prediction"


def test_direct_cli_export_uses_only_the_standard_library(tmp_path: Path) -> None:
    old_pred, old_metric = _attempt("case-a", "old", PATCH.replace("+b\n", "+old\n"), "failed")
    new_pred, new_metric = _attempt("case-a", "new", PATCH)
    batch = tmp_path / "batch"
    _write_attempts(batch, [old_pred, new_pred], [new_metric, old_metric])
    script = Path(__file__).resolve().parents[1] / "analyst_patch_preds.py"
    output = tmp_path / "analyst-preds.jsonl"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), str(batch), "--arm", "arm", "--out", str(output)],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["record_id"] == "new" and len(rows) == 1
    assert "1 identical to the graded patch" in result.stdout
