"""The frozen draw has to be reproducible, capped, and stratified."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from opencollab_eval.experiment.task_sampling import (
    FrameRow,
    allocate_by_largest_remainder,
    capped_repository_shares,
    draw_ordered_list,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = "opencollab_eval.commands.draw_task_suite"
SUITE = ROOT / "experiment" / "suite"
FRAME = SUITE / "frame-verified-500.csv"
DIFFICULTIES = ("<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours")


def _frame_rows(path: Path = FRAME) -> list[FrameRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [FrameRow(row["instance_id"], row["repo"], row["difficulty"]) for row in csv.DictReader(handle)]


def _synthetic_frame() -> list[FrameRow]:
    sizes = {"big/one": 200, "mid/two": 60, "mid/three": 30, "small/four": 10}
    return [
        FrameRow(f"{repo.replace('/', '__')}-{index}", repo, DIFFICULTIES[index % len(DIFFICULTIES)])
        for repo, size in sizes.items()
        for index in range(size)
    ]


def test_shares_are_capped_and_the_excess_is_redistributed() -> None:
    shares = capped_repository_shares({"a": 462, "b": 150, "c": 88, "d": 300}, 0.30)
    assert shares["a"] == pytest.approx(0.30)
    assert shares["d"] == pytest.approx(0.30)
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares["b"] / shares["c"] == pytest.approx(150 / 88)


def test_a_cap_the_frame_cannot_meet_is_refused() -> None:
    with pytest.raises(ValueError):
        capped_repository_shares({"a": 1, "b": 1, "c": 1}, 0.30)


def test_allocation_sums_to_the_total_and_respects_what_exists() -> None:
    allocation = allocate_by_largest_remainder({"a": 0.5, "b": 0.4, "c": 0.1}, 20, available={"a": 3, "b": 40, "c": 40})
    assert sum(allocation.values()) == 20
    assert allocation["a"] == 3


def test_the_same_seed_reproduces_the_draw_and_another_seed_does_not() -> None:
    rows = _synthetic_frame()
    first = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110)
    again = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110)
    other = draw_ordered_list(rows, seed=20260902, head_size=100, total_size=110)
    assert first.ordered == again.ordered
    assert first.ordered != other.ordered


def test_the_draw_is_ordered_longer_than_the_suite_and_has_no_repeats() -> None:
    draw = draw_ordered_list(_synthetic_frame(), seed=20260901, head_size=100, total_size=110)
    assert len(draw.ordered) == 110
    assert len(set(draw.ordered)) == 110


def test_no_repository_takes_more_than_the_cap_of_the_suite() -> None:
    rows = _frame_rows()
    draw = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110)
    by_id = {row.instance_id: row for row in rows}
    counts = Counter(by_id[instance_id].repo for instance_id in draw.ordered[:100])
    assert max(counts.values()) == 30, counts
    assert counts["django/django"] == 30


def test_the_subset_is_a_capped_stratified_subset_of_the_suite() -> None:
    rows = _frame_rows()
    by_id = {row.instance_id: row for row in rows}
    suite = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110).ordered[:100]
    subset = draw_ordered_list(
        [by_id[instance_id] for instance_id in suite],
        seed=20260901,
        head_size=30,
        total_size=50,
        namespace="subset",
    )
    assert len(subset.ordered) == 50
    assert set(subset.ordered) <= set(suite)
    counts = Counter(by_id[instance_id].repo for instance_id in subset.ordered[:30])
    assert max(counts.values()) <= 9, counts
    assert max(Counter(by_id[i].repo for i in subset.ordered).values()) <= 15


def test_every_difficulty_stratum_the_frame_has_reaches_the_suite() -> None:
    rows = _frame_rows()
    by_id = {row.instance_id: row for row in rows}
    suite = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110).ordered[:100]
    assert set(Counter(by_id[instance_id].difficulty for instance_id in suite)) >= {
        "<15 min fix",
        "15 min - 1 hour",
        "1-4 hours",
    }


def _run_script(tmp_path: Path, *, seed: int, images: Path) -> Path:
    out = tmp_path / f"out-{seed}"
    subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--frame",
            str(FRAME),
            "--out-dir",
            str(out),
            "--seed",
            str(seed),
            "--images",
            str(images),
            "--images-host",
            "test",
        ],
        check=True,
        capture_output=True,
        cwd=ROOT,
    )
    return out


def test_the_script_reproduces_its_artifacts_byte_for_byte(tmp_path: Path) -> None:
    images = tmp_path / "images.txt"
    images.write_text(
        "\n".join(
            f"swebench/sweb.eval.x86_64.{row.instance_id.replace('__', '_1776_')}:latest" for row in _frame_rows()
        ),
        encoding="utf-8",
    )
    first = _run_script(tmp_path, seed=20260901, images=images)
    again = _run_script(tmp_path / "twice", seed=20260901, images=images)
    other = _run_script(tmp_path, seed=20260902, images=images)
    for name in ("suite-100.csv", "subset-50.csv", "subset-30.csv"):
        assert (first / name).read_bytes() == (again / name).read_bytes()
        assert (first / name).read_bytes() != (other / name).read_bytes()
        assert (first / name).read_bytes() == (SUITE / name).read_bytes()
    manifest = json.loads((first / "sampling-manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 20260901
    assert manifest["preflight"]["skipped"] == []


def test_an_absent_image_is_skipped_with_its_reason_and_replaced(tmp_path: Path) -> None:
    rows = _frame_rows()
    first_drawn = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110).ordered[0]
    images = tmp_path / "images.txt"
    images.write_text(
        "\n".join(
            f"swebench/sweb.eval.x86_64.{row.instance_id.replace('__', '_1776_')}:latest"
            for row in rows
            if row.instance_id != first_drawn
        ),
        encoding="utf-8",
    )
    out = _run_script(tmp_path, seed=20260901, images=images)
    manifest = json.loads((out / "sampling-manifest.json").read_text(encoding="utf-8"))
    assert [entry["instance_id"] for entry in manifest["preflight"]["skipped"]] == [first_drawn]
    assert manifest["preflight"]["skipped"][0]["reason"] == "image absent on run host"
    with (out / "suite-100.csv").open(encoding="utf-8", newline="") as handle:
        suite = [row["instance_id"] for row in csv.DictReader(handle)]
    assert len(suite) == 100
    assert first_drawn not in suite


def test_the_committed_manifest_matches_the_committed_files() -> None:
    manifest = json.loads((SUITE / "sampling-manifest.json").read_text(encoding="utf-8"))
    with (SUITE / "suite-100.csv").open(encoding="utf-8", newline="") as handle:
        suite = list(csv.DictReader(handle))
    with (SUITE / "subset-50.csv").open(encoding="utf-8", newline="") as handle:
        subset = list(csv.DictReader(handle))
    with (SUITE / "subset-30.csv").open(encoding="utf-8", newline="") as handle:
        head = list(csv.DictReader(handle))
    assert len(suite) == manifest["sizes"]["suite"] == 100
    assert len(subset) == manifest["sizes"]["subset"] == 50
    assert len(head) == manifest["sizes"]["subset_head"] == 30
    assert {row["instance_id"] for row in subset} <= {row["instance_id"] for row in suite}
    for repo, counts in manifest["repository_counts"].items():
        assert counts["suite"] == sum(1 for row in suite if row["repo"] == repo)
        assert counts["subset"] == sum(1 for row in subset if row["repo"] == repo)
        assert counts["subset_head"] == sum(1 for row in head if row["repo"] == repo)
        assert counts["suite"] <= manifest["cap"] * manifest["sizes"]["suite"]
        assert counts["subset"] <= manifest["cap"] * manifest["sizes"]["subset"]


def test_a_repository_with_nothing_left_is_allocated_nothing() -> None:
    allocation = allocate_by_largest_remainder({"a": 0.5, "b": 0.5}, 10, available={"a": 40})
    assert allocation == {"a": 10, "b": 0}


def test_the_tail_block_fills_even_when_the_head_block_exhausted_a_repository() -> None:
    rows = [
        FrameRow(f"{repo}-{index}", repo, DIFFICULTIES[index % len(DIFFICULTIES)])
        for repo, size in {"big/one": 200, "mid/two": 60, "mid/three": 30, "small/four": 10}.items()
        for index in range(size)
    ]
    draw = draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110)
    assert len(draw.ordered) == 110


def test_the_short_subset_is_a_prefix_of_the_long_one_byte_for_byte() -> None:
    long_rows = (SUITE / "subset-50.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    short_rows = (SUITE / "subset-30.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(long_rows) == 51
    assert len(short_rows) == 31
    assert short_rows == long_rows[:31]


def test_growing_the_subset_never_moves_a_row_that_was_already_run() -> None:
    rows = _frame_rows()
    by_id = {row.instance_id: row for row in rows}
    suite = [by_id[i] for i in draw_ordered_list(rows, seed=20260901, head_size=100, total_size=110).ordered[:100]]
    thirty = draw_ordered_list(suite, seed=20260901, head_size=30, total_size=30, namespace="subset").ordered
    fifty = draw_ordered_list(suite, seed=20260901, head_size=30, total_size=50, namespace="subset").ordered
    assert fifty[:30] == thirty
