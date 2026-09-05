"""What ``cell_report`` reads off a cell, for each arm that writes seat files.

The reason this file exists: every DW run in ``dw-subset50`` reported zero
seats, zero snapshots and ``delivered=False``, and nothing anywhere said the
seat files had never been opened. Two independent silent failures produced
that -- a glob that matches only the team's file names, and a role field the
workflow runtime writes as one generic value -- so both are pinned here, on a
tree built to the shape the driver actually writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.batch_spec import SpecError, load_spec

PRECHECK_TERMINAL = (
    "budget exhausted before model call: conservative input reservation "
    "requires 118192 of 101794 remaining tokens"
)
POSTCALL_TERMINAL = "budget exceeded after model call: 2000512 tokens used"


def _messages(assistant_turns: int, tool_name: str | None = None) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "fix it"}]
    for i in range(assistant_turns):
        message: dict = {"role": "assistant", "content": f"turn {i}"}
        if tool_name:
            message["tool_calls"] = [
                {"id": f"c{i}", "function": {"name": tool_name, "arguments": "{}"}}
            ]
        messages.append(message)
    return messages


def _seat_file(
    directory: Path,
    filename: str,
    *,
    aid: int,
    role: str,
    tokens: int,
    assistant: int,
    terminal: str = "",
    tool_name: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "aid": aid,
        "role": role,
        "session_state": {
            "used_tokens": tokens,
            "step_count": assistant,
            "terminal_reason": terminal,
        },
        "messages": _messages(assistant, tool_name),
    }
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")
    # The autosave journal sits beside every snapshot and must never be read
    # as a seat of its own.
    (directory / f"{filename}.journal").write_text("{}\n", encoding="utf-8")


def _runtime_dir(cell: Path, arm: str, instance_id: str) -> Path:
    return (
        cell / f"logs-{arm}" / instance_id / "trajectories" / "solver-aa" / "runtime-bb"
    )


def _write_metrics(cell: Path, records: list[dict]) -> None:
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "metrics.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


@pytest.fixture
def dw_cell(tmp_path: Path) -> Path:
    """One healthy DW run and one that stopped with no brief, as the driver writes them."""
    cell = tmp_path / "dw-cell"

    healthy = _runtime_dir(cell, "self-collaboration", "django__django-12262")
    _seat_file(healthy, "000_analyst.json", aid=0, role="workflow_agent",
               tokens=209_224, assistant=16, tool_name="file_write")
    _seat_file(healthy, "001_coder-r1.json", aid=1, role="workflow_agent",
               tokens=93_604, assistant=11, tool_name="apply_patch")
    _seat_file(healthy, "002_tester-r1.json", aid=2, role="workflow_agent",
               tokens=111_088, assistant=11)
    _seat_file(healthy, "003_analyst-adjudicate-r1.json", aid=3,
               role="workflow_agent", tokens=38_137, assistant=5)

    degenerate = _runtime_dir(cell, "self-collaboration", "astropy__astropy-12907")
    _seat_file(degenerate, "000_analyst.json", aid=0, role="workflow_agent",
               tokens=1_898_206, assistant=61, terminal=PRECHECK_TERMINAL)
    _seat_file(degenerate, "001_analyst.json", aid=1, role="workflow_agent",
               tokens=0, assistant=0, terminal=POSTCALL_TERMINAL)

    _write_metrics(cell, [
        {
            "instance_id": "django__django-12262",
            "run_summary": {"status": "completed", "reason": None,
                            "tokens": 452_053, "steps": 43},
            "workflow_result": {"status": "done",
                                "tree_snapshots": [{"after": "analyze"},
                                                   {"after": "implement:r1"}]},
            "submitted_patch_chars": 812,
        },
        {
            "instance_id": "astropy__astropy-12907",
            "run_summary": {"status": "stopped",
                            "reason": "analyst produced no structured brief",
                            "tokens": 1_898_206, "steps": 61},
            "workflow_result": {"status": "error",
                                "error": "analyst produced no structured brief",
                                "tree_snapshots": [{"after": "analyze"}]},
            "submitted_patch_chars": 0,
        },
    ])
    return cell


def _rows(cell: Path) -> dict[str, cell_report.RunRow]:
    rows = cell_report.run_rows(cell, "self-collaboration")
    return {r.instance_id: r for r in rows}


# --- E3: the seat files are found at all ------------------------------------ #


def test_a_workflow_run_s_seat_files_are_found(dw_cell: Path) -> None:
    """The team glob ``agent_*.json`` matches none of ``000_analyst.json`` & co."""
    rows = _rows(dw_cell)

    assert len(rows["django__django-12262"].seats) == 4
    assert len(rows["astropy__astropy-12907"].seats) == 2
    # The journal sidecars are not seats.
    assert set(rows["django__django-12262"].seats) == {"0", "1", "2", "3"}


def test_a_team_run_s_seat_files_are_still_found(tmp_path: Path) -> None:
    """Positive control for the widened glob: the team layout is unchanged."""
    cell = tmp_path / "team-cell"
    runtime = _runtime_dir(cell, "team", "astropy__astropy-14096")
    _seat_file(runtime, "agent_0_analyst-f44ceb062e35.json", aid=0, role="analyst",
               tokens=500_000, assistant=20, tool_name="message_agent")
    _seat_file(runtime, "agent_1_coder-c84a9e3ad144.json", aid=1, role="coder",
               tokens=300_000, assistant=12, tool_name="apply_patch")
    _write_metrics(cell, [{
        "instance_id": "astropy__astropy-14096",
        "run_summary": {"status": "completed", "tokens": 800_000, "steps": 32},
        "tree_snapshots": [{"after": "analyst"}],
    }])

    rows = {r.instance_id: r for r in cell_report.run_rows(cell, "team")}
    seats = rows["astropy__astropy-14096"].seats

    assert {s.role for s in seats.values()} == {"analyst", "coder"}
    assert seats["0"].msg_agent == 20
    assert seats["1"].writes == 12
    assert rows["astropy__astropy-14096"].delivered is True
    assert rows["astropy__astropy-14096"].tree_snapshots == 1


# --- E4: the seat's role, and therefore delivery ---------------------------- #


def test_a_workflow_seat_takes_its_role_from_its_file_name(dw_cell: Path) -> None:
    """The runtime stamps every workflow seat ``workflow_agent``.

    Finding the files is not enough: with one generic role for all of them,
    ``delivered`` -- a coder or tester seat that spent tokens and spoke -- is
    false on every run whatever the run did.
    """
    seats = _rows(dw_cell)["django__django-12262"].seats

    assert [seats[str(i)].role for i in range(4)] == [
        "analyst", "coder", "tester", "analyst",
    ]
    assert seats["1"].tokens == 93_604
    assert seats["1"].assistant == 11


def test_delivery_is_true_on_a_run_that_reached_its_coder(dw_cell: Path) -> None:
    rows = _rows(dw_cell)

    assert rows["django__django-12262"].delivered is True
    # The degenerate run seated no coder and no tester, so it really did not
    # deliver -- the same reading, now for the right reason.
    assert rows["astropy__astropy-12907"].delivered is False


def test_the_cell_summary_states_delivery_for_a_workflow_arm(dw_cell: Path) -> None:
    rows = cell_report.run_rows(dw_cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert summary["delivered"] == 1
    assert summary["alpha"] == pytest.approx(0.5)
    assert summary["ci95"] is not None
    assert all(bound is not None for bound in summary["ci95"])


# --- E5: the snapshots a workflow records inside its own result ------------- #


def test_tree_snapshots_fall_back_to_the_workflow_result(dw_cell: Path) -> None:
    """A workflow writes its boundaries under ``workflow_result``, not at the top.

    Read only at the top level a DW run shows zero -- indistinguishable from an
    arm that records no boundaries at all.
    """
    rows = _rows(dw_cell)

    assert rows["django__django-12262"].tree_snapshots == 2
    assert rows["astropy__astropy-12907"].tree_snapshots == 1


# --- E7: the two things spelt "budget" ------------------------------------- #


def test_the_pre_call_reservation_and_a_real_overspend_are_separate_columns(
    dw_cell: Path,
) -> None:
    """Only one of the two is a run whose outcome the cap chose.

    ``"budget" in terminal`` matched both, so the ladder's capped column could
    not tell an estimator that refused to enter a call from a seat that
    actually spent past its allowance.
    """
    row = _rows(dw_cell)["astropy__astropy-12907"]

    assert row.cap_hit == ["0", "1"]
    assert row.cap_hit_precheck == ["0"]
    assert row.cap_hit_postcall == ["1"]

    summary = cell_report.summarize(
        cell_report.run_rows(dw_cell, "self-collaboration"), None, team=True
    )
    assert summary["cap_hit"] == ["astropy__astropy-12907"]
    assert summary["cap_hit_precheck"] == ["astropy__astropy-12907"]
    assert summary["cap_hit_postcall"] == ["astropy__astropy-12907"]
    # A run that never touched either marker is in none of the three.
    assert "django__django-12262" not in summary["cap_hit"]


def test_a_stopped_run_is_a_valid_denominator_and_lands_in_the_precheck_column(
    dw_cell: Path,
) -> None:
    """The two halves of E1/E2 as ``cell_report`` sees them.

    Once the generator records the analyst failure as ``stopped`` with a reason
    (rather than ``completed`` with none), the run must still count in the
    denominator -- it reached the model, it just stopped -- and its stop must
    be attributed to the pre-call reservation rather than to a real overspend.
    """
    rows = cell_report.run_rows(dw_cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)
    row = {r.instance_id: r for r in rows}["astropy__astropy-12907"]

    assert row.status == "stopped"
    assert row.reason == "analyst produced no structured brief"
    assert row.valid is True
    assert summary["invalid"] == []
    assert summary["valid"] == 2
    assert summary["statuses"] == {"completed": 1, "stopped": 1}
    assert row.instance_id in summary["cap_hit_precheck"]


def test_the_rendered_report_names_both_kinds_of_stop(dw_cell: Path) -> None:
    rows = cell_report.run_rows(dw_cell, "self-collaboration")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=True), [])

    assert "stopped by the pre-call reservation: 1" in text
    assert "overspent after a call returned:     1" in text


# --- E6: delivery is read on the DW arm without widening TEAM_ARMS ---------- #


def test_the_reporting_set_covers_the_workflow_arms() -> None:
    from opencollab_eval.generation.gen_prediction_batch import (
        DELIVERY_READABLE_ARMS,
        TEAM_ARMS,
        WORKFLOW_ARMS,
    )

    assert TEAM_ARMS <= DELIVERY_READABLE_ARMS
    assert set(WORKFLOW_ARMS) <= DELIVERY_READABLE_ARMS
    assert "self-collaboration" in DELIVERY_READABLE_ARMS
    # The spec validator's set is the one that must NOT grow: it is what
    # requires a cell and a rung of every arm in it.
    assert "self-collaboration" not in TEAM_ARMS


def test_a_dw_spec_still_loads_with_no_cell_and_no_rung(tmp_path: Path) -> None:
    """The mutation control for E6.

    Widening ``TEAM_ARMS`` instead of adding a reporting set would make this
    spec fail with "needs a 'cell'" -- and every DW batch file in
    ``experiment/batches`` is written exactly like it.
    """
    spec_dir = tmp_path / "batches"
    spec_dir.mkdir(parents=True)
    path = spec_dir / "dw.yaml"
    body = (
        "name: dwspec\n"
        "host: h\n"
        "arm: self-collaboration\n"
        "suite: tiny\n"
        "rows: {start: 12, stop: 14}\n"
        "budget_per_seat: 2000000\n"
        "max_steps: 100\n"
        "timeout: 5400\n"
        "concurrency: 3\n"
        "model_env: configs/.env\n"
        "env:\n"
        '  OPENCOLLAB_LLM_STREAM_CHAT: "true"\n'
        '  OPENCOLLAB_REASONING_EFFORT: "max"\n'
        '  OPENCOLLAB_WRITE_NUDGE_MODE: "off"\n'
        "pins:\n"
        "  opencollab: " + "a" * 40 + "\n"
        "  opencollab_eval: " + "b" * 40 + "\n"
    )
    path.write_text(body, encoding="utf-8")

    spec = load_spec(path)
    assert spec.arm == "self-collaboration"
    assert spec.cell is None

    # And the rule that set enforces is still enforced: a cell on a non-team
    # arm is still refused.
    path.write_text(body.replace("suite: tiny\n", "cell: facts-v2\nsuite: tiny\n"),
                    encoding="utf-8")
    with pytest.raises(SpecError, match="takes no 'cell'"):
        load_spec(path)


# --- E8: the single arm's seat, which lives nowhere the globs look ---------- #


def _single_cell(tmp_path: Path) -> Path:
    """One capped and one clean single run, laid out the way the driver writes.

    The single arm autosaves ``<cell>/agent-<hex>/agent.json`` at the batch
    root and leaves only a ``driver.log`` under ``logs-single/<instance>/``, so
    the instance-to-seat tie exists only in ``metrics.jsonl``
    (``trajectory_path``). The recorded path is absolute and was written on the
    host that ran the batch, so the fixture keeps it foreign on purpose.
    """
    cell = tmp_path / "single-cell"
    capped_dir = cell / "agent-5387e0b241a64637a4d4e08fed968cb2"
    clean_dir = cell / "agent-222f65639eb74cc0b1b6020e14714fa7"
    _seat_file(capped_dir, "agent.json", aid=-1, role="swe_agent",
               tokens=1_968_907, assistant=53, terminal=PRECHECK_TERMINAL)
    _seat_file(clean_dir, "agent.json", aid=-1, role="swe_agent",
               tokens=378_313, assistant=21, terminal="submitted")
    for instance in ("matplotlib__matplotlib-25775", "django__django-13933"):
        (cell / "logs-single" / instance).mkdir(parents=True, exist_ok=True)
        (cell / "logs-single" / instance / "driver.log").write_text("", encoding="utf-8")
    _write_metrics(cell, [
        {
            "instance_id": "matplotlib__matplotlib-25775",
            "trajectory_path": f"/home/someone/oc-team-smoke/single/{capped_dir.name}",
            "run_summary": {"status": "stopped", "reason": PRECHECK_TERMINAL,
                            "tokens": 1_968_907, "steps": 53},
            "submitted_patch_chars": 3231,
        },
        {
            "instance_id": "django__django-13933",
            "trajectory_path": f"/home/someone/oc-team-smoke/single/{clean_dir.name}",
            "run_summary": {"status": "completed", "reason": None,
                            "tokens": 378_313, "steps": 21},
            "submitted_patch_chars": 792,
        },
    ])
    return cell


def test_a_single_run_s_seat_file_is_found_through_its_record(tmp_path: Path) -> None:
    """``logs-single/<instance>/`` holds a log and no trajectories at all.

    Globbing there returned no seats for every single run -- the same reading a
    run that spent nothing produces -- so nothing said the file had not been
    opened.
    """
    rows = {r.instance_id: r for r in cell_report.run_rows(_single_cell(tmp_path), "single")}

    assert len(rows["matplotlib__matplotlib-25775"].seats) == 1
    assert rows["matplotlib__matplotlib-25775"].seats["-1"].tokens == 1_968_907
    assert rows["django__django-13933"].seats["-1"].role == "swe_agent"


def test_the_single_arm_splits_its_cap_stops_by_the_same_rule_as_the_others(
    tmp_path: Path,
) -> None:
    """The defect this file's E7 case pinned for DW, on the arm it was missing.

    The capped run carries the pre-call reservation verbatim in its own
    ``reason``, yet ``cap_hit_precheck`` was empty for the whole arm, so the
    ladder's capped-alone column read zero on Single and non-zero on DW and
    Team from the same stop.
    """
    cell = _single_cell(tmp_path)
    rows = cell_report.run_rows(cell, "single")
    summary = cell_report.summarize(rows, None, team=False)
    row = {r.instance_id: r for r in rows}["matplotlib__matplotlib-25775"]

    assert row.cap_hit == ["-1"]
    assert row.cap_hit_precheck == ["-1"]
    assert row.cap_hit_postcall == []
    assert summary["cap_hit"] == ["matplotlib__matplotlib-25775"]
    assert summary["cap_hit_precheck"] == ["matplotlib__matplotlib-25775"]
    assert summary["cap_hit_postcall"] == []
    # The clean run is in none of the three, and delivery stays undefined:
    # a single agent has nobody to deliver to.
    assert "django__django-13933" not in summary["cap_hit"]
    assert summary["delivered"] is None


def test_a_single_run_that_overspent_after_a_call_lands_in_the_other_column(
    tmp_path: Path,
) -> None:
    """Positive control for the split: the same terminal strings, same sides."""
    cell = tmp_path / "single-postcall"
    seat_dir = cell / "agent-030b1adf169e4492ba0981ff53bf56c0"
    _seat_file(seat_dir, "agent.json", aid=-1, role="swe_agent",
               tokens=2_000_512, assistant=40, terminal=POSTCALL_TERMINAL)
    _write_metrics(cell, [{
        "instance_id": "django__django-13933",
        "trajectory_path": f"/home/someone/oc-team-smoke/x/{seat_dir.name}",
        "run_summary": {"status": "stopped", "reason": POSTCALL_TERMINAL,
                        "tokens": 2_000_512, "steps": 40},
    }])

    summary = cell_report.summarize(cell_report.run_rows(cell, "single"), None, team=False)

    assert summary["cap_hit_postcall"] == ["django__django-13933"]
    assert summary["cap_hit_precheck"] == []


def test_the_single_report_prints_both_kinds_of_stop(tmp_path: Path) -> None:
    """The counts have to be visible on the arm too, not only in the JSON."""
    rows = cell_report.run_rows(_single_cell(tmp_path), "single")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=False), [])

    assert "stopped by the pre-call reservation: 1" in text
    assert "overspent after a call returned:     0" in text


def test_a_record_with_no_trajectory_path_yields_no_seat(tmp_path: Path) -> None:
    """An older batch's rows must degrade to the old reading, never a wrong one.

    Nothing else in the cell names an instance, so a seat guessed by globbing
    ``agent-*`` would be attributed to whichever run happened to be read first.
    """
    cell = tmp_path / "single-old"
    _seat_file(cell / "agent-deadbeef", "agent.json", aid=-1, role="swe_agent",
               tokens=999, assistant=3, terminal=PRECHECK_TERMINAL)
    _write_metrics(cell, [{
        "instance_id": "django__django-13933",
        "run_summary": {"status": "completed", "tokens": 999, "steps": 3},
    }])

    rows = cell_report.run_rows(cell, "single")

    assert rows[0].seats == {}
    assert rows[0].cap_hit_precheck == []


def test_the_workflow_arms_do_not_take_the_single_arm_s_path(dw_cell: Path) -> None:
    """The mutation control for E8.

    A DW or team record's ``trajectory_path`` names an ``orchestration.jsonl``
    or ``trajectory.jsonl`` *file* inside ``logs-<arm>/``, not a seat
    directory, so resolving every arm through the record would drop those arms
    to zero seats. The dispatch is by arm, and this is the arm that must not
    move.
    """
    assert "self-collaboration" not in cell_report.SEAT_AT_BATCH_ROOT_ARMS
    assert "team" not in cell_report.SEAT_AT_BATCH_ROOT_ARMS

    rows = _rows(dw_cell)
    assert len(rows["django__django-12262"].seats) == 4
    assert rows["astropy__astropy-12907"].cap_hit_precheck == ["0"]
