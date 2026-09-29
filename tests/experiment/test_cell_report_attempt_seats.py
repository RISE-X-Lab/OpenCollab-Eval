"""A run's seats come from the attempt its record describes.

A resumed launch can run an instance a second time in the same out-dir, and
then two attempt directories sit under one instance's ``trajectories`` root
(``solver-<hex>/runtime-<hex>``, random hex). The report used to read the seat
snapshots of both and keep, per seat, whichever file sorted last -- so every
metrics row of that instance, whichever attempt it described, took its
delegation columns from the attempt whose random directory name sorted last.
That is how ``s2dual-judge-qwen38-pro36`` (resumed; 9 instances ran twice)
printed every_delegate 11/34 where the counted attempts give 16/34.

The record names its own attempt (``trajectory_path``, written on the host).
An absent named attempt leaves the seat snapshot missing. Older records can
use the instance directory only when it contains one task.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _attempt(cell: Path, solver: str, runtime: str, arm: str = "team") -> Path:
    directory = cell / f"logs-{arm}" / IID / "trajectories" / solver / runtime
    directory.mkdir(parents=True, exist_ok=True)
    # The run's own node list, which is where the report learns that coder_a
    # and coder_b are the seats the entry seat could hand work to.
    nodes = {
        "type": "assigned.topology_nodes",
        "payload": {
            "nodes": [
                {"aid": 0, "role": "adopter", "entry": True},
                {"aid": 1, "role": "coder_a", "entry": False},
                {"aid": 2, "role": "coder_b", "entry": False},
            ]
        },
    }
    (directory / "trajectory.jsonl").write_text(json.dumps(nodes) + "\n", encoding="utf-8")
    return directory


def _host_path(cell: Path, solver: str, runtime: str, arm: str = "team") -> str:
    # Written on the machine that ran the batch: a different prefix, the same tail.
    return f"/home/someone/{cell.name}/logs-{arm}/{IID}/trajectories/{solver}/{runtime}/trajectory.jsonl"


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
        {
            "instance_id": IID,
            "run_summary": {"status": "failed", "tokens": 1_300_000, "steps": 40},
            "trajectory_path": _host_path(cell, "solver-aa", "runtime-aa"),
        },
        {
            "instance_id": IID,
            "run_summary": {"status": "completed", "tokens": 700_000, "steps": 25},
            "trajectory_path": _host_path(cell, "solver-zz", "runtime-zz"),
        },
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
    _seat(
        _attempt(cell, "solver-aa", "runtime-aa"),
        "agent_0_adopter-1.json",
        aid=0,
        role="adopter",
        tokens=100,
        assistant=1,
    )
    _seat(
        _attempt(cell, "solver-aa", "runtime-aa"),
        "agent_1_coder_a-1.json",
        aid=1,
        role="coder_a",
        tokens=200,
        assistant=2,
    )
    (cell / "metrics.jsonl").write_text(
        json.dumps({"instance_id": IID, "run_summary": {"status": "completed", "tokens": 300, "steps": 3}}) + "\n",
        encoding="utf-8",
    )
    row = cell_report.run_rows(cell, "team")[0]
    assert set(row.seats) == {"0", "1"}
    assert row.delivered is True


def test_a_named_attempt_missing_from_the_cell_has_no_snapshot(tmp_path: Path) -> None:
    cell = tmp_path / "partly-pulled"
    _seat(
        _attempt(cell, "solver-aa", "runtime-aa"),
        "agent_0_adopter-1.json",
        aid=0,
        role="adopter",
        tokens=100,
        assistant=1,
    )
    (cell / "metrics.jsonl").write_text(
        json.dumps(
            {
                "instance_id": IID,
                "run_summary": {"status": "completed", "tokens": 100, "steps": 1},
                "trajectory_path": _host_path(cell, "solver-qq", "runtime-qq"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    row = cell_report.run_rows(cell, "team")[0]
    assert row.tokens == 100
    assert row.seats == {}
    assert row.seat_snapshot_found is False
    assert cell_report.summarize([row], None, team=True)["seat_snapshot_missing"] == [IID]


@pytest.mark.parametrize("arm", ["self-collaboration", "self-collaboration-reading-analyst"])
@pytest.mark.parametrize("trace_in_task", [True, False])
def test_workflow_rows_and_merge_use_the_named_attempt(tmp_path: Path, arm: str, trace_in_task: bool) -> None:
    cell = tmp_path / arm
    root = cell / f"logs-{arm}" / IID / "trajectories"
    first_task = root / "solver-zz"
    second_task = root / "solver-aa"
    first_runtime = first_task / "runtime-first"
    second_runtime = second_task / "runtime-second"
    _seat(first_runtime, "000_analyst.json", aid=0, role="workflow_agent", tokens=10, assistant=1)
    _seat(second_runtime, "000_analyst.json", aid=0, role="workflow_agent", tokens=99, assistant=1)
    # The trace can be beside the role snapshots or at their task parent.
    first_trace = (first_task if trace_in_task else first_runtime) / "orchestration.jsonl"
    second_trace = (second_task if trace_in_task else second_runtime) / "orchestration.jsonl"
    first_trace.write_text("{}\n", encoding="utf-8")
    second_trace.write_text("{}\n", encoding="utf-8")
    records = [
        {
            "instance_id": IID,
            "run_summary": {"status": "completed", "tokens": 10},
            "trajectory_path": f"/remote/trajectories/{first_trace.relative_to(root)}",
        },
        {
            "instance_id": IID,
            "run_summary": {"status": "completed", "tokens": 99},
            "trajectory_path": str(second_trace),
        },
    ]
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    rows = cell_report.run_rows(cell, arm)
    assert [(row.tokens, row.seats["0"].tokens) for row in rows] == [(10, 10), (99, 99)]
    selected = cell_report.merge_attempts([("batch", rows)])[0]
    assert (selected.attempt, selected.tokens, selected.seats["0"].tokens) == (2, 99, 99)


@pytest.mark.parametrize("arm", ["team", "self-collaboration", "self-collaboration-reading-analyst"])
def test_missing_named_attempt_never_borrows_a_workflow_or_team_seat(tmp_path: Path, arm: str) -> None:
    cell = tmp_path / arm
    existing = _attempt(cell, "solver-aa", "runtime-aa", arm)
    filename = "agent_0_analyst.json" if arm == "team" else "000_analyst.json"
    role = "analyst" if arm == "team" else "workflow_agent"
    _seat(existing, filename, aid=0, role=role, tokens=99, assistant=1)
    record = {
        "instance_id": IID,
        "run_summary": {"status": "completed", "tokens": 10},
        "trajectory_path": "/remote/trajectories/solver-missing/runtime/orchestration.jsonl",
    }
    (cell / "metrics.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    row = cell_report.run_rows(cell, arm)[0]
    assert row.tokens == 10
    assert row.seats == {}
    assert row.seat_snapshot_found is False


def test_missing_named_runtime_with_existing_task_has_no_snapshot(tmp_path: Path) -> None:
    cell = tmp_path / "partly-pulled-task"
    existing = _attempt(cell, "solver-aa", "runtime-other")
    _seat(existing, "agent_0_analyst.json", aid=0, role="analyst", tokens=99, assistant=1)
    record = {
        "instance_id": IID,
        "run_summary": {"status": "completed", "tokens": 10},
        "trajectory_path": "/remote/trajectories/solver-aa/runtime-missing/trajectory.jsonl",
    }
    (cell / "metrics.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    row = cell_report.run_rows(cell, "team")[0]
    assert row.tokens == 10
    assert row.seats == {}
    assert row.seat_snapshot_found is False


@pytest.mark.parametrize("arm", ["self-collaboration", "self-collaboration-reading-analyst"])
@pytest.mark.parametrize("path_kind", ["task", "legacy", "runtime"])
def test_multiple_runtime_snapshots_need_a_runtime_path(tmp_path: Path, arm: str, path_kind: str) -> None:
    cell = tmp_path / arm
    task = cell / f"logs-{arm}" / IID / "trajectories" / "solver-aa"
    first = task / "runtime-aa"
    second = task / "runtime-zz"
    _seat(first, "000_analyst.json", aid=0, role="workflow_agent", tokens=10, assistant=1)
    _seat(first, "001_coder.json", aid=1, role="workflow_agent", tokens=20, assistant=1)
    _seat(second, "000_analyst.json", aid=0, role="workflow_agent", tokens=99, assistant=1)
    record = {"instance_id": IID, "run_summary": {"status": "completed", "tokens": 30}}
    if path_kind == "task":
        record["trajectory_path"] = "/remote/trajectories/solver-aa/orchestration.jsonl"
    elif path_kind == "runtime":
        record["trajectory_path"] = "/remote/trajectories/solver-aa/runtime-aa/orchestration.jsonl"
    (cell / "metrics.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    row = cell_report.run_rows(cell, arm)[0]
    assert row.tokens == 30
    if path_kind == "runtime":
        assert {aid: seat.tokens for aid, seat in row.seats.items()} == {"0": 10, "1": 20}
        assert row.seat_snapshot_found is True
    else:
        assert row.seats == {}
        assert row.seat_snapshot_found is False


def test_legacy_record_with_multiple_attempts_has_ambiguous_seats(tmp_path: Path) -> None:
    cell = tmp_path / "legacy-ambiguous"
    for solver, tokens in (("solver-aa", 10), ("solver-zz", 99)):
        runtime = _attempt(cell, solver, "runtime-aa")
        _seat(runtime, "agent_0_analyst.json", aid=0, role="analyst", tokens=tokens, assistant=1)
    (cell / "metrics.jsonl").write_text(
        json.dumps({"instance_id": IID, "run_summary": {"status": "completed", "tokens": 10}}) + "\n",
        encoding="utf-8",
    )

    row = cell_report.run_rows(cell, "team")[0]
    assert row.tokens == 10
    assert row.seats == {}
    assert row.seat_snapshot_found is False
