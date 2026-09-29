"""Attempt-specific seat attribution for the standalone batch scanner."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from test_scan_batch import SCRIPTS, _seat, _write, scan_batch


def _write_metrics(cell, records):
    os.makedirs(cell, exist_ok=True)
    with open(os.path.join(cell, "metrics.jsonl"), "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _completed(instance_id, path=None):
    record = {"instance_id": instance_id, "run_summary": {"status": "completed", "tokens": 10}}
    if path is not None:
        record["trajectory_path"] = path
    return record


def test_named_team_attempts_follow_each_metrics_row_and_cli(tmp_path):
    cell = str(tmp_path / "team-attempts")
    instance_id = "task__1"
    root = os.path.join(cell, "logs-team", instance_id, "trajectories")
    late = os.path.join(root, "solver-zz", "runtime-zz")
    early = os.path.join(root, "solver-aa", "runtime-aa")
    _write(os.path.join(late, "agent_0_analyst.json"), _seat(0, "analyst", 10, tool="message_agent"))
    _write(os.path.join(late, "agent_1_coder.json"), _seat(1, "coder", 20))
    _write(os.path.join(early, "agent_0_analyst.json"), _seat(0, "analyst", 99))
    records = [
        _completed(instance_id, "/remote/trajectories/solver-zz/runtime-zz/trajectory.jsonl"),
        _completed(instance_id, "/remote/trajectories/solver-aa/runtime-aa/trajectory.jsonl"),
    ]
    _write_metrics(cell, records)
    result = scan_batch.scan_cell(cell)
    assert [(run["tokens_by_aid"], run["delegated"]) for run in result["runs"]] == [
        ({0: 10, 1: 20}, True),
        ({0: 99}, False),
    ]
    assert (result["alpha_num"], result["alpha_den"]) == (1, 2)

    output = tmp_path / "team.json"
    subprocess.run(
        [sys.executable, "-S", os.path.join(SCRIPTS, "scan_batch.py"), cell, "--json", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert [(run["metrics_line"], run["tokens_by_aid"], run["delegated"]) for run in document["runs"]] == [
        (1, {"0": 10, "1": 20}, True),
        (2, {"0": 99}, False),
    ]
    assert (document["cells"][0]["alpha_num"], document["cells"][0]["alpha_den"]) == (1, 2)

    records[0]["trajectory_path"] = "/remote/trajectories/solver-missing/runtime-missing/trajectory.jsonl"
    _write_metrics(cell, records)
    missing = scan_batch.scan_cell(cell)
    assert missing["runs"][0]["validity_class"] == scan_batch.NO_TRAJECTORY
    assert missing["runs"][0]["trajectory_found"] is False
    assert missing["runs"][0]["tokens_by_aid"] == {}
    assert missing["runs"][1]["tokens_by_aid"] == {0: 99}
    assert any("solver-missing" in problem for problem in missing["problems"])


def test_unnamed_attempt_requires_one_seat_directory(tmp_path):
    cell = str(tmp_path / "legacy")
    instance_id = "task__1"
    root = os.path.join(cell, "logs-team", instance_id, "trajectories")
    first = os.path.join(root, "solver-aa", "runtime-aa")
    _write(os.path.join(first, "agent_0_analyst.json"), _seat(0, "analyst", 10, tool="message_agent"))
    _write(os.path.join(first, "agent_1_coder.json"), _seat(1, "coder", 20))
    _write_metrics(cell, [_completed(instance_id)])
    unique = scan_batch.scan_cell(cell)
    assert unique["runs"][0]["trajectory_dir"] == first
    assert unique["runs"][0]["n_agent_files"] == 2

    other = os.path.join(root, "solver-zz", "runtime-zz")
    _write(os.path.join(other, "agent_0_analyst.json"), _seat(0, "analyst", 99))
    ambiguous = scan_batch.scan_cell(cell)
    assert ambiguous["runs"][0]["validity_class"] == scan_batch.NO_TRAJECTORY
    assert ambiguous["runs"][0]["tokens_by_aid"] == {}
    assert any("multiple unnamed seat directories" in problem for problem in ambiguous["problems"])


def test_workflow_task_log_resolves_nested_runtime_and_single_local_path(tmp_path):
    cell = str(tmp_path / "workflow-attempts")
    instance_id = "flow__1"
    root = os.path.join(cell, "logs-self-collaboration", instance_id, "trajectories")
    first = os.path.join(root, "solver-zz", "runtime-zz")
    second = os.path.join(root, "solver-aa", "runtime-aa")
    _write(os.path.join(first, "000_analyst.json"), _seat(0, "workflow_agent", 10, tool="message_agent"))
    _write(os.path.join(first, "001_coder-r1.json"), _seat(1, "workflow_agent", 20))
    _write(os.path.join(second, "000_analyst.json"), _seat(0, "workflow_agent", 99))
    _write_metrics(
        cell,
        [
            _completed(instance_id, "/remote/trajectories/solver-zz/orchestration.jsonl"),
            _completed(instance_id, "/remote/trajectories/solver-aa/runtime-aa/orchestration.jsonl"),
        ],
    )
    workflow = scan_batch.scan_cell(cell)
    assert [run["trajectory_dir"] for run in workflow["runs"]] == [first, second]
    assert [run["tokens_by_role"] for run in workflow["runs"]] == [
        {"analyst": 10, "coder": 20},
        {"analyst": 99},
    ]
    assert (workflow["alpha_num"], workflow["alpha_den"]) == (1, 2)

    single = str(tmp_path / "single-local")
    seat_dir = os.path.join(single, "agent-local")
    _write(os.path.join(seat_dir, "agent.json"), _seat(-1, "swe_agent", 7))
    _write_metrics(single, [_completed("solo__1", os.path.join(seat_dir, "trajectory.jsonl"))])
    assert scan_batch.scan_cell(single)["runs"][0]["tokens_by_aid"] == {-1: 7}
