"""The frozen draw has to be reproducible, capped, and stratified."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab_eval.experiment.task_sampling import (
    FrameRow,
    allocate_by_largest_remainder,
    capped_repository_shares,
    draw_ordered_list,
    order_frame,
)

ROOT = Path(__file__).resolve().parents[2]
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






def test_the_frame_order_is_a_seeded_permutation_of_the_whole_frame() -> None:
    frame = _synthetic_frame()
    first = order_frame(frame, seed=20260901)
    assert first == order_frame(frame, seed=20260901)
    assert sorted(first) == sorted(row.instance_id for row in frame)
    assert first != order_frame(frame, seed=20260902)
    assert first != tuple(row.instance_id for row in frame)
    assert first != draw_ordered_list(frame, seed=20260901, head_size=10, total_size=20).ordered[:20]


def test_the_frame_order_script_skips_an_absent_image_and_reproduces_itself(tmp_path: Path) -> None:
    frame = _synthetic_frame()[:40]
    frame_path = tmp_path / "frame.csv"
    with frame_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["instance_id", "repo", "difficulty"])
        writer.writeheader()
        writer.writerows({"instance_id": r.instance_id, "repo": r.repo, "difficulty": r.difficulty} for r in frame)
    from opencollab_eval.commands.draw_task_suite import image_reference

    missing = frame[3].instance_id
    images = tmp_path / "images.txt"
    images.write_text("\n".join(image_reference(r.instance_id) for r in frame if r.instance_id != missing) + "\n")
    command = [
        sys.executable,
        "-m",
        "opencollab_eval.commands.order_frame",
        "--frame",
        str(frame_path),
        "--out-dir",
        str(tmp_path / "out"),
        "--seed",
        "7",
        "--images",
        str(images),
        "--images-host",
        "test",
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    first = (tmp_path / "out" / "frame-ordered.csv").read_bytes()
    manifest = json.loads((tmp_path / "out" / "frame-ordered-manifest.json").read_text())
    rows = list(csv.DictReader((tmp_path / "out" / "frame-ordered.csv").open(encoding="utf-8", newline="")))
    assert [r["order"] for r in rows] == [str(i) for i in range(1, len(frame))]
    assert missing not in {r["instance_id"] for r in rows}
    assert manifest["preflight"]["skipped"] == [
        {"instance_id": missing, "image": image_reference(missing), "reason": "image absent on run host"}
    ]
    assert manifest["sizes"] == {"frame": len(frame), "ordered": len(frame) - 1, "skipped": 1}
    assert [r["instance_id"] for r in rows] == [i for i in order_frame(frame, seed=7) if i != missing]
    subprocess.run(command, check=True, cwd=ROOT)
    assert (tmp_path / "out" / "frame-ordered.csv").read_bytes() == first
