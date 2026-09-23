"""A team run's seats come from the attempt its record describes.

A resumed launch can run an instance a second time in the same out-dir, and
then two attempt directories sit under one instance's ``trajectories`` root
(``solver-<hex>/runtime-<hex>``, random hex). The report used to read the seat
snapshots of both and keep, per seat, whichever file sorted last -- so every
metrics row of that instance, whichever attempt it described, took its
delegation columns from the attempt whose random directory name sorted last.
That is how ``s2dual-judge-qwen38-pro36`` (resumed; 9 instances ran twice)
printed every_delegate 11/34 where the counted attempts give 16/34.

The record names its own attempt (``trajectory_path``, written on the host),
so a team row now reads that directory only, and falls back to the old glob
when the record names none or the directory is not in the pulled cell.
"""

from __future__ import annotations

import json
from pathlib import Path

from opencollab_eval.experiment import cell_report

IID = "instance_flipt-io__flipt-aa11"


def _seat(directory: Path, filename: str, *, aid: int, role: str, tokens: int, assistant: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    messages: list[dict] = [{"role": "user", "content": "task"}]
    for i in range(assistant):
        messages.append({"role": "assistant", "content": f"turn {i}"})
    payload = {
        "aid": aid,
        "role": role,
        "session_state": {"used_tokens": tokens, "step_count": assistant, "terminal_reason": ""},
        "messages": messages,
    }
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")


def _attempt(cell: Path, solver: str, runtime: str) -> Path:
    directory = cell / "logs-team" / IID / "trajectories" / solver / runtime
    directory.mkdir(parents=True, exist_ok=True)
    # The run's own node list, which is where the report learns that coder_a
    # and coder_b are the seats the entry seat could hand work to.
    nodes = {"type": "assigned.topology_nodes", "payload": {"nodes": [
        {"aid": 0, "role": "adopter", "entry": True},
        {"aid": 1, "role": "coder_a", "entry": False},
        {"aid": 2, "role": "coder_b", "entry": False},
    ]}}
    (directory / "trajectory.jsonl").write_text(json.dumps(nodes) + "\n", encoding="utf-8")
    return directory


def _host_path(cell: Path, solver: str, runtime: str) -> str:
    # Written on the machine that ran the batch: a different prefix, the same tail.
    return f"/home/someone/{cell.name}/logs-team/{IID}/trajectories/{solver}/{runtime}/trajectory.jsonl"


def _two_attempt_cell(tmp_path: Path) -> Path:
    """First attempt: a Coder worked. Second (its directory sorts last): no Coder did."""
    cell = tmp_path / "resumed-cell"
    first = _attempt(cell, "solver-aa", "runtime-aa")
    _seat(first, "agent_0_adopter-1.json", aid=0, role="adopter", tokens=400_000, assistant=10)
    _seat(first, "agent_1_coder_a-1.json", aid=1, role="coder_a", tokens=900_000, assistant=30)
    second = _attempt(cell, "solver-zz", "runtime-zz")
    _seat(second, "agent_0_adopter-2.json", aid=0, role="adopter", tokens=700_000, assistant=25)
    _seat(second, "agent_1_coder_a-2.json", aid=1, role="coder_a", tokens=0, assistant=0)
    cell.mkdir(parents=True, exist_ok=True)
    records = [
        {"instance_id": IID, "run_summary": {"status": "failed", "tokens": 1_300_000, "steps": 40},
         "trajectory_path": _host_path(cell, "solver-aa", "runtime-aa")},
        {"instance_id": IID, "run_summary": {"status": "completed", "tokens": 700_000, "steps": 25},
         "trajectory_path": _host_path(cell, "solver-zz", "runtime-zz")},
    ]
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return cell


def test_each_row_reads_the_seats_of_its_own_attempt(tmp_path: Path) -> None:
    rows = cell_report.run_rows(_two_attempt_cell(tmp_path), "team")
    first, second = rows
    assert first.seats["1"].tokens == 900_000
    assert first.delivered is True
    assert second.seats["0"].tokens == 700_000
    assert second.seats["1"].tokens == 0
    assert second.delivered is False


def test_a_row_does_not_borrow_a_seat_from_the_other_attempt(tmp_path: Path) -> None:
    cell = _two_attempt_cell(tmp_path)
    # The second attempt never seated coder_a at all.
    (_attempt(cell, "solver-zz", "runtime-zz") / "agent_1_coder_a-2.json").unlink()
    second = cell_report.run_rows(cell, "team")[1]
    assert set(second.seats) == {"0"}
    assert second.delivered is False


def test_a_record_without_a_trajectory_path_still_reads_the_instance_root(tmp_path: Path) -> None:
    cell = tmp_path / "old-cell"
    _seat(_attempt(cell, "solver-aa", "runtime-aa"), "agent_0_adopter-1.json",
          aid=0, role="adopter", tokens=100, assistant=1)
    _seat(_attempt(cell, "solver-aa", "runtime-aa"), "agent_1_coder_a-1.json",
          aid=1, role="coder_a", tokens=200, assistant=2)
    (cell / "metrics.jsonl").write_text(json.dumps(
        {"instance_id": IID, "run_summary": {"status": "completed", "tokens": 300, "steps": 3}}) + "\n",
        encoding="utf-8")
    row = cell_report.run_rows(cell, "team")[0]
    assert set(row.seats) == {"0", "1"}
    assert row.delivered is True


def test_a_named_attempt_missing_from_the_cell_falls_back_to_the_instance_root(tmp_path: Path) -> None:
    cell = tmp_path / "partly-pulled"
    _seat(_attempt(cell, "solver-aa", "runtime-aa"), "agent_0_adopter-1.json",
          aid=0, role="adopter", tokens=100, assistant=1)
    (cell / "metrics.jsonl").write_text(json.dumps(
        {"instance_id": IID, "run_summary": {"status": "completed", "tokens": 100, "steps": 1},
         "trajectory_path": _host_path(cell, "solver-qq", "runtime-qq")}) + "\n", encoding="utf-8")
    row = cell_report.run_rows(cell, "team")[0]
    assert set(row.seats) == {"0"}
