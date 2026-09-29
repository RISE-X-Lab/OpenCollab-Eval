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
from hashlib import sha256
from pathlib import Path

import pytest

from opencollab_eval.candidate_bytes import encode_candidate_jsonl
from opencollab_eval.engine.swe_eval_records import RecordInputFormatError
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


def _identified_attempt(
    record_id: str, tokens: int, *, instance_id: str = IID, status: str = "completed"
) -> tuple[dict, dict]:
    patch = f"patch for {record_id}"
    identity = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": sha256(patch.encode()).hexdigest(),
    }
    metric = {**identity, "run_summary": {"status": status, "tokens": tokens}}
    prediction = {**identity, "model_patch": patch, "workflow_metric": metric}
    return prediction, metric


def _write_rows(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.mark.parametrize("arm", ["team", "self-collaboration", "single"])
def test_recovered_metrics_follow_prediction_attempt_order(tmp_path: Path, arm: str) -> None:
    cell = tmp_path / arm
    cell.mkdir()
    old_prediction, old_metric = _identified_attempt("old", 10)
    new_prediction, new_metric = _identified_attempt("new", 99)
    other_prediction, other_metric = _identified_attempt("other", 27, instance_id="another-instance")
    legacy_metric = {"instance_id": IID, "run_summary": {"status": "failed", "tokens": 5}}
    _write_rows(cell / f"preds-{arm}.jsonl", [old_prediction, other_prediction, new_prediction])
    _write_rows(cell / "predictions.jsonl", [new_prediction, old_prediction])
    _write_rows(cell / "metrics.jsonl", [legacy_metric, new_metric, other_metric])

    # The missing old sidecar comes from its persisted prediction, before a later
    # recovery appends that sidecar at the end of the physical metrics file.
    initial = cell_report.run_rows(cell, arm)
    assert [(row.record_id, row.tokens) for row in initial] == [
        ("", 5), ("old", 10), ("other", 27), ("new", 99)
    ]
    _write_rows(cell / "metrics.jsonl", [legacy_metric, new_metric, other_metric, old_metric])
    for _ in range(2):
        rows = cell_report.run_rows(cell, arm)
        assert [(row.record_id, row.tokens) for row in rows] == [
            ("", 5), ("old", 10), ("other", 27), ("new", 99)
        ]
        selected = {row.instance_id: row for row in cell_report.merge_attempts([(arm, rows)])}
        assert (selected[IID].record_id, selected[IID].attempt, selected[IID].attempts) == ("new", 3, 3)
        assert selected[IID].attempt_tokens_total == 114
        assert selected["another-instance"].tokens == 27


def test_recovery_does_not_pair_conflicting_or_unrelated_metrics(tmp_path: Path) -> None:
    cell = tmp_path / "conflicts"
    cell.mkdir()
    old_prediction, old_metric = _identified_attempt("old", 10)
    new_prediction, new_metric = _identified_attempt("new", 99)
    unrelated = {**old_metric, "instance_id": "another-instance", "run_summary": {"status": "completed", "tokens": 700}}
    wrong_patch = {
        **old_metric,
        "patch_sha256": new_metric["patch_sha256"],
        "run_summary": {"status": "completed", "tokens": 800},
    }
    _write_rows(cell / "predictions.jsonl", [old_prediction, new_prediction])
    _write_rows(cell / "metrics.jsonl", [unrelated, wrong_patch, new_metric])

    rows = cell_report.run_rows(cell)
    # The conflicting sidecar remains visible as metrics-only history. It does
    # not displace the correctly identified later attempt or its token count.
    assert [(row.instance_id, row.record_id, row.tokens) for row in rows] == [
        ("another-instance", "old", 700),
        (IID, "old", 800),
        (IID, "new", 99),
    ]
    selected = {row.instance_id: row for row in cell_report.merge_attempts([("batch", rows)])}
    assert selected[IID].record_id == "new"


def test_torn_metric_line_recovers_embedded_attempt_and_reports_damage(tmp_path: Path) -> None:
    cell = tmp_path / "torn"
    cell.mkdir()
    old_prediction, old_metric = _identified_attempt("old", 10)
    new_prediction, new_metric = _identified_attempt("new", 99)
    _write_rows(cell / "preds-team.jsonl", [old_prediction, new_prediction])
    metrics = cell / "metrics.jsonl"
    torn_prefix = encode_candidate_jsonl(old_metric)[:28]
    metrics.write_bytes(encode_candidate_jsonl(new_metric) + torn_prefix + b"\n" + encode_candidate_jsonl(old_metric))

    with pytest.warns(RuntimeWarning, match="damaged JSONL lines \\[2\\]"):
        rows = cell_report.run_rows(cell)
    assert [(row.record_id, row.tokens) for row in rows] == [("old", 10), ("new", 99)]
    assert cell_report.merge_attempts([("batch", rows)])[0].tokens == 99
    assert torn_prefix + b"\n" in metrics.read_bytes()


def test_torn_metric_line_without_recovery_source_is_an_error(tmp_path: Path) -> None:
    cell = tmp_path / "unrecoverable"
    cell.mkdir()
    (cell / "metrics.jsonl").write_text('{"instance_id":\n', encoding="utf-8")
    with pytest.raises(RecordInputFormatError, match="without prediction recovery source"):
        cell_report.run_rows(cell)


def test_unrelated_damaged_metric_line_is_not_skipped(tmp_path: Path) -> None:
    cell = tmp_path / "unrelated-damage"
    cell.mkdir()
    prediction, metric = _identified_attempt("new", 99)
    _write_rows(cell / "preds-team.jsonl", [prediction])
    (cell / "metrics.jsonl").write_bytes(b'{"instance_id": "unrelated"\n' + encode_candidate_jsonl(metric))

    with pytest.raises(RecordInputFormatError, match="no matching prediction prefix"):
        cell_report.run_rows(cell)


def test_recovered_order_keeps_each_attempts_own_trajectory(tmp_path: Path) -> None:
    cell = tmp_path / "trajectory-recovery"
    cell.mkdir()
    old_prediction, old_metric = _identified_attempt("old", 10)
    new_prediction, new_metric = _identified_attempt("new", 99)
    for name, metric, tokens in (("aa", old_metric, 10), ("zz", new_metric, 99)):
        directory = _attempt(cell, f"solver-{name}", f"runtime-{name}")
        _seat(directory, f"agent_0_adopter-{name}.json", aid=0, role="adopter", tokens=tokens, assistant=1)
        metric["trajectory_path"] = _host_path(cell, f"solver-{name}", f"runtime-{name}")
    _write_rows(cell / "preds-team.jsonl", [old_prediction, new_prediction])
    _write_rows(cell / "metrics.jsonl", [new_metric, old_metric])

    rows = cell_report.run_rows(cell)
    assert [(row.record_id, row.seats["0"].tokens) for row in rows] == [("old", 10), ("new", 99)]
    assert cell_report.merge_attempts([("batch", rows)])[0].seats["0"].tokens == 99


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
