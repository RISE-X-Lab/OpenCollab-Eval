"""The final image-available suite retains its declared repository cap."""

import csv
import json
from collections import Counter

import pytest

from opencollab_eval.commands.draw_task_suite import image_reference, main
from opencollab_eval.experiment.task_sampling import FrameRow, draw_ordered_list


def _inputs(tmp_path, missing):
    rows = [FrameRow(f"{repo}-{i}", repo, "small")
            for repo, count in {"large": 200, "medium": 100, "other": 100, "small": 100}.items()
            for i in range(count)]
    draw = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=130, cap=0.30)
    by_id = {row.instance_id: row for row in rows}
    absent = set(missing(draw.ordered, by_id))
    frame = tmp_path / "frame.csv"
    with frame.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["instance_id", "repo", "difficulty"])
        writer.writerows((row.instance_id, row.repo, row.difficulty) for row in rows)
    images = tmp_path / "images.txt"
    images.write_text("\n".join(image_reference(row.instance_id) for row in rows if row.instance_id not in absent))
    out = tmp_path / "out"
    argv = ["--frame", str(frame), "--out-dir", str(out), "--seed", "20260901",
            "--draw-size", "130", "--images", str(images)]
    return argv, out, draw, absent


def _suite(out):
    with (out / "suite-100.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_reserve_replacement_preserves_repository_cap(tmp_path):
    argv, out, _, absent = _inputs(
        tmp_path, lambda order, by_id: [key for key in order[:100] if by_id[key].repo != "large"][:6]
    )
    assert main(argv) == 0
    selected = _suite(out)
    assert len(selected) == 100
    assert max(Counter(row["repo"] for row in selected).values()) <= 30
    assert not absent.intersection(row["instance_id"] for row in selected)
    manifest = json.loads((out / "sampling-manifest.json").read_text())
    assert any(row["reason"] == "repository cap reached" for row in manifest["preflight"]["skipped"])
    original = (out / "suite-100.csv").read_bytes()
    assert main(argv) == 0
    assert (out / "suite-100.csv").read_bytes() == original


def test_unfiltered_draw_keeps_existing_order(tmp_path):
    argv, out, draw, _ = _inputs(tmp_path, lambda *_: [])
    assert main(argv) == 0
    assert [row["instance_id"] for row in _suite(out)] == list(draw.ordered[:100])
    assert json.loads((out / "sampling-manifest.json").read_text())["preflight"]["skipped"] == []


def test_insufficient_capped_reserve_creates_no_partial_suite(tmp_path):
    argv, out, _, _ = _inputs(
        tmp_path, lambda order, by_id: [key for key in order if by_id[key].repo != "large"]
    )
    with pytest.raises(SystemExit, match="only 30 of 100"):
        main(argv)
    assert not out.exists()
