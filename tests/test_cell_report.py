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


# --- E9: a seat file that was never found, said out loud -------------------- #


def test_a_single_run_whose_seat_is_not_found_is_named_in_the_summary(
    tmp_path: Path,
) -> None:
    """The gap E8's fix left: the lookup can still come back empty in silence.

    ``_single_agent_files`` returns ``[]`` on four ordinary conditions -- the
    record has no ``trajectory_path``, the directory was renamed on the way to
    this machine, the batch predates the autosave, or ``agent.json`` is absent
    -- and every one of them reads as "zero seats, no cap, nothing delivered",
    which is what a run that sailed through its budget also reads as. So the
    row has to say which of the two it is.
    """
    cell = tmp_path / "single-halffound"
    found_dir = cell / "agent-222f65639eb74cc0b1b6020e14714fa7"
    _seat_file(found_dir, "agent.json", aid=-1, role="swe_agent",
               tokens=378_313, assistant=21, terminal="submitted")
    _write_metrics(cell, [
        {
            "instance_id": "django__django-13933",
            "trajectory_path": f"/home/someone/single/{found_dir.name}",
            "run_summary": {"status": "completed", "tokens": 378_313, "steps": 21},
        },
        {
            # Same shape, but the directory the record names is not here.
            "instance_id": "sympy__sympy-24213",
            "trajectory_path": "/home/someone/single/agent-4e0f8c1b",
            "run_summary": {"status": "completed", "tokens": 511_004, "steps": 30},
        },
    ])

    rows = cell_report.run_rows(cell, "single")
    by_id = {r.instance_id: r for r in rows}
    summary = cell_report.summarize(rows, None, team=False)

    assert by_id["django__django-13933"].seat_snapshot_found is True
    assert by_id["sympy__sympy-24213"].seat_snapshot_found is False
    assert summary["seat_snapshot_found"] == 1
    assert summary["seat_snapshot_missing"] == ["sympy__sympy-24213"]
    assert summary["seat_snapshot_missing_count"] == 1
    # The cap columns are unchanged: the rule that fills them is not touched,
    # so the run with no file is in none of them -- which is exactly why the
    # count above has to exist.
    assert summary["cap_hit"] == []
    assert summary["cap_hit_precheck"] == []


def test_every_seat_found_leaves_the_missing_list_empty(tmp_path: Path) -> None:
    """Positive control: the flag is not simply always false."""
    rows = cell_report.run_rows(_single_cell(tmp_path), "single")
    summary = cell_report.summarize(rows, None, team=False)

    assert all(r.seat_snapshot_found for r in rows)
    assert summary["seat_snapshot_found"] == 2
    assert summary["seat_snapshot_missing"] == []
    assert summary["seat_snapshot_missing_count"] == 0


def test_the_report_warns_in_the_text_when_a_seat_is_missing(tmp_path: Path) -> None:
    """A JSON key nobody prints is not visible; the run report is what is read."""
    cell = tmp_path / "single-warn"
    _write_metrics(cell, [{
        "instance_id": "sympy__sympy-24213",
        "run_summary": {"status": "completed", "tokens": 511_004, "steps": 30},
    }])
    rows = cell_report.run_rows(cell, "single")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=False), [])

    assert "SEAT SNAPSHOT NOT FOUND" in text
    assert "1/1" in text
    assert "sympy__sympy-24213" in text.split("SEAT SNAPSHOT NOT FOUND", 1)[1]


def test_the_report_stays_quiet_when_every_seat_is_found(tmp_path: Path) -> None:
    """The mutation control for the warning: it must not print unconditionally."""
    rows = cell_report.run_rows(_single_cell(tmp_path), "single")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=False), [])

    assert "SEAT SNAPSHOT NOT FOUND" not in text


def test_a_workflow_run_with_no_seat_files_is_named_too(dw_cell: Path) -> None:
    """The same silence exists on the arms that glob, so the flag covers them.

    A DW run whose ``trajectories`` tree was never written -- the run died
    before the first seat, or the tree was not pulled -- reads as zero seats,
    ``delivered=False`` and no cap, which is a reading the arm's own healthy
    runs can also produce.
    """
    _write_metrics(dw_cell, [
        {
            "instance_id": "django__django-12262",
            "run_summary": {"status": "completed", "tokens": 452_053, "steps": 43},
        },
        {
            "instance_id": "pydata__xarray-4075",
            "run_summary": {"status": "stopped", "reason": "container gone",
                            "tokens": 0, "steps": 0},
        },
    ])

    rows = cell_report.run_rows(dw_cell, "self-collaboration")
    by_id = {r.instance_id: r for r in rows}
    summary = cell_report.summarize(rows, None, team=True)
    text = cell_report.render(rows, summary, [])

    assert by_id["django__django-12262"].seat_snapshot_found is True
    assert by_id["pydata__xarray-4075"].seat_snapshot_found is False
    assert summary["seat_snapshot_missing"] == ["pydata__xarray-4075"]
    assert "SEAT SNAPSHOT NOT FOUND" in text
    # Delivery, the cap columns and the denominator are untouched.
    assert summary["delivered"] == 1
    assert summary["valid"] == 2
    assert summary["cap_hit"] == []


def test_best_of_n_keeps_its_seats_out_of_the_batch_root(tmp_path: Path) -> None:
    """Why the batch-root set stays at one arm.

    ``arm_registry.ARMS`` also lists ``best-of-n``, and it runs the single
    arm's own ``run_agent``, so "it is a one-seat arm, add it" is the obvious
    move. Its generator does not pass the batch root: each candidate is given
    ``candidate_run_directory``, nested four levels below the batch root and
    keyed by instance and candidate index, so ``<cell>/<basename>`` resolves to
    nothing. And ``combine_metrics`` copies the *selected* candidate's record,
    so the run's ``trajectory_path`` names one of N seats -- reading it would
    under-count this arm's cap stops rather than fix them.
    """
    from opencollab_eval.generation.gen_prediction_best_of_n import (
        candidate_run_directory,
    )

    root = tmp_path / "bon-cell"
    candidate = candidate_run_directory(root, "django__django-13933", 1)

    assert candidate.parent != root
    assert candidate.relative_to(root).parts == (
        ".opencollab", "best-of-n", "django__django-13933", "candidate-1",
    )
    assert "best-of-n" not in cell_report.SEAT_AT_BATCH_ROOT_ARMS
    assert cell_report.SEAT_AT_BATCH_ROOT_ARMS == frozenset({"single"})


# --- E10: a role seated twice in one run ------------------------------------ #
#
# The scripted workflow seats its analyst twice -- ``000_analyst`` to write the
# brief and ``003_analyst-adjudicate-r1`` to rule on the tester's report -- and
# the report keyed the render's per-role lookup by role alone, so the second
# seat replaced the first and the analyst column printed the adjudication's
# spend as if it were the whole role's. On the smoke DW batch that printed
# 48,695 where the run spent 1,995,025.


def test_a_role_seated_twice_is_summed_not_overwritten(dw_cell: Path) -> None:
    row = _rows(dw_cell)["django__django-12262"]

    # 000_analyst 209,224 + 003_analyst-adjudicate-r1 38,137.
    assert row.role_tokens["analyst"] == 247_361
    assert row.role_tokens["coder"] == 93_604
    assert row.role_tokens["tester"] == 111_088
    assert row.role_seats["analyst"] == 2


def test_the_rendered_analyst_column_is_the_role_s_whole_spend(dw_cell: Path) -> None:
    """The column is what is read; a JSON key nobody prints is not the fix."""
    rows = cell_report.run_rows(dw_cell, "self-collaboration")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=True), [])
    line = next(l for l in text.splitlines() if "django__django-12262" in l)

    assert "247,361" in line
    # The last seat's own spend must no longer stand in for the role.
    assert "38,137" not in line


def test_the_role_totals_are_checked_against_the_workflow_s_own_ledger(
    tmp_path: Path,
) -> None:
    """``workflow_result.seat_spend`` is the workflow's own per-seat ledger.

    Summing the seat files reproduces it exactly on every DW run measured, so
    a disagreement means one of the two readings is wrong and the report has to
    say so rather than print whichever it happened to compute.
    """
    cell = tmp_path / "dw-ledger"
    runtime = _runtime_dir(cell, "self-collaboration", "django__django-12262")
    _seat_file(runtime, "000_analyst.json", aid=0, role="workflow_agent",
               tokens=209_224, assistant=16)
    _seat_file(runtime, "003_analyst-adjudicate-r1.json", aid=3,
               role="workflow_agent", tokens=38_137, assistant=5)
    _write_metrics(cell, [{
        "instance_id": "django__django-12262",
        "run_summary": {"status": "completed", "tokens": 247_361, "steps": 21},
        "workflow_result": {"status": "done", "seat_cap": 2_000_000,
                            "seat_spend": {"analyst": 247_361}},
    }])

    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_spend_recorded == {"analyst": 247_361}
    assert rows[0].seat_spend_agrees is True
    assert summary["seat_spend_disagrees"] == []

    # Mutation control: break the ledger and the disagreement must surface.
    _write_metrics(cell, [{
        "instance_id": "django__django-12262",
        "run_summary": {"status": "completed", "tokens": 247_361, "steps": 21},
        "workflow_result": {"status": "done", "seat_cap": 2_000_000,
                            "seat_spend": {"analyst": 38_137}},
    }])
    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_spend_agrees is False
    assert summary["seat_spend_disagrees"] == ["django__django-12262"]


# --- E11: the seat allowance, and what a workflow seat's stop actually says -- #
#
# On the smoke DW batch three runs of four had every seat's ``terminal_reason``
# reading ``"interrupted by user"`` and printed ``-`` in the cap column while
# one of them had spent 1,995,025 of a 2,000,000-token analyst seat. Reading
# the runtime settles both halves of that:
#
#   * "interrupted by user" is not a stop at the budget. It is the
#     structured-output capture: ``workflow_structured.py:137-138`` sets a
#     cancel event on a successful ``structured_output`` call and
#     ``session_run.py:604-606`` writes that reason when the next precheck sees
#     it. Those three runs ended by a *successful* capture.
#   * The allowance those seats were drawing against is never in the seat file.
#     ``session_state`` has ``used_tokens`` and no ``max_budget_tokens``; the
#     ceiling is recorded once per session in the ``session_terminal`` event
#     (``session_run.py:333-380``) in the run's own event log --
#     ``trajectory.jsonl`` on the single and team arms, ``orchestration.jsonl``
#     on a scripted workflow.
#
# So the cap column stays what it was, a stop the runtime *recorded*, and the
# report gains the recorded ceiling beside the spend, so a run that finished
# with 0.25% of its seat left is no longer spelt exactly like one that finished
# with 90% left.

STRUCTURED_CAPTURE_TERMINAL = "interrupted by user"
SPENT_TERMINAL = "budget exceeded: 2000000 tokens used"


def _event_log(directory: Path, filename: str, terminals: list[dict]) -> None:
    """The run's ordered event log, with one ``session_terminal`` per session."""
    directory.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "llm_call", "payload": {"aid": 0}}]
    rows += [{"type": "session_terminal", "payload": t} for t in terminals]
    (directory / filename).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


@pytest.fixture
def dw_capped_cell(tmp_path: Path) -> Path:
    """A DW run that finished on a nearly-spent analyst seat, as the driver writes it.

    The numbers are ``smoke-falsecap-dw``'s astropy run: the analyst is seated
    twice for 1,946,330 + 48,695 against a 2,000,000 seat, the adjudication
    session's own ceiling is what the seat had left, and every seat stopped on
    a structured capture.
    """
    cell = tmp_path / "dw-capped"
    runtime = _runtime_dir(cell, "self-collaboration", "astropy__astropy-14369")
    _seat_file(runtime, "000_analyst.json", aid=0, role="workflow_agent",
               tokens=1_946_330, assistant=62, terminal=STRUCTURED_CAPTURE_TERMINAL)
    _seat_file(runtime, "001_coder-r1.json", aid=1, role="workflow_agent",
               tokens=63_791, assistant=6, terminal=STRUCTURED_CAPTURE_TERMINAL)
    _seat_file(runtime, "002_tester-r1.json", aid=2, role="workflow_agent",
               tokens=210_509, assistant=16, terminal=STRUCTURED_CAPTURE_TERMINAL)
    _seat_file(runtime, "003_analyst-adjudicate-r1.json", aid=3, role="workflow_agent",
               tokens=48_695, assistant=4, terminal=STRUCTURED_CAPTURE_TERMINAL)
    _event_log(runtime, "orchestration.jsonl", [
        {"aid": 0, "used_tokens": 1_946_330, "max_budget_tokens": 2_000_000,
         "terminal_reason": STRUCTURED_CAPTURE_TERMINAL},
        {"aid": 1, "used_tokens": 63_791, "max_budget_tokens": 2_000_000,
         "terminal_reason": STRUCTURED_CAPTURE_TERMINAL},
        {"aid": 2, "used_tokens": 210_509, "max_budget_tokens": 2_000_000,
         "terminal_reason": STRUCTURED_CAPTURE_TERMINAL},
        {"aid": 3, "used_tokens": 48_695, "max_budget_tokens": 53_670,
         "terminal_reason": STRUCTURED_CAPTURE_TERMINAL},
    ])
    _write_metrics(cell, [{
        "instance_id": "astropy__astropy-14369",
        "run_summary": {"status": "completed", "reason": None,
                        "tokens": 2_269_325, "steps": 88, "duration_s": 1265.15},
        "workflow_result": {
            "status": "done",
            "seat_cap": 2_000_000,
            "seat_spend": {"analyst": 1_995_025, "coder": 63_791, "tester": 210_509},
            "tree_snapshots": [{"after": "analyze"}, {"after": "implement:r1"}],
        },
    }])
    return cell


def test_a_structured_capture_stop_is_not_a_cap_stop(dw_capped_cell: Path) -> None:
    """The rule that must NOT change: "interrupted by user" stays out of the cap columns."""
    rows = cell_report.run_rows(dw_capped_cell, "self-collaboration")

    assert rows[0].cap_hit == []
    assert rows[0].cap_hit_precheck == []
    assert rows[0].cap_hit_postcall == []


def test_the_seat_allowance_is_read_from_the_session_terminal_event(
    dw_capped_cell: Path,
) -> None:
    """``max_budget_tokens`` is in the event log and nowhere in the seat file."""
    row = cell_report.run_rows(dw_capped_cell, "self-collaboration")[0]

    assert row.seats["0"].cap == 2_000_000
    # The adjudication session was handed what the seat had left, not the seat.
    assert row.seats["3"].cap == 53_670
    assert row.seat_cap == 2_000_000


def test_a_run_that_finished_on_a_nearly_spent_seat_says_how_much_was_left(
    dw_capped_cell: Path,
) -> None:
    """The defect: a seat at 99.75% and a seat at 3% both printed ``-``."""
    row = cell_report.run_rows(dw_capped_cell, "self-collaboration")[0]

    assert row.role_headroom["analyst"] == 4_975
    assert row.role_headroom["coder"] == 1_936_209
    assert row.seat_headroom_min == 4_975

    rows = cell_report.run_rows(dw_capped_cell, "self-collaboration")
    text = cell_report.render(rows, cell_report.summarize(rows, None, team=True), [])
    assert "4,975" in text
    assert "smallest seat headroom left" in text


def test_the_workflow_s_own_budget_exhausted_verdict_is_read(tmp_path: Path) -> None:
    """``self_collaboration.py:466-467`` decides a seat is spent and records it.

    ``workflow_result.status == "budget_exhausted"`` is the workflow's own
    finding, written when ``exhausted(seat)`` stops a round. Nothing read it,
    so an arm whose script stopped for want of budget reported the same status
    vocabulary as one that finished.
    """
    cell = tmp_path / "dw-exhausted"
    runtime = _runtime_dir(cell, "self-collaboration", "sympy__sympy-15875")
    _seat_file(runtime, "000_analyst.json", aid=0, role="workflow_agent",
               tokens=2_000_000, assistant=60, terminal=STRUCTURED_CAPTURE_TERMINAL)
    _event_log(runtime, "orchestration.jsonl", [
        {"aid": 0, "used_tokens": 2_000_000, "max_budget_tokens": 2_000_000,
         "terminal_reason": STRUCTURED_CAPTURE_TERMINAL},
    ])
    _write_metrics(cell, [{
        "instance_id": "sympy__sympy-15875",
        "run_summary": {"status": "completed", "tokens": 2_000_000, "steps": 60},
        "workflow_result": {"status": "budget_exhausted", "seat_cap": 2_000_000,
                            "seat_spend": {"analyst": 2_000_000}},
    }])

    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].budget_exhausted is True
    assert rows[0].seat_headroom_min == 0
    assert summary["seat_budget_exhausted"] == ["sympy__sympy-15875"]
    assert "seat budget exhausted (the workflow's own verdict): 1" in cell_report.render(
        rows, summary, []
    )


def test_the_other_two_recorded_cap_strings_are_classified_too(tmp_path: Path) -> None:
    """``session_run.py`` writes four budget stops, not two.

    :616 ``budget exceeded: N tokens used`` is the precheck firing on spend
    already made; :625 ``team budget exceeded: ...`` is the aggregate ceiling.
    Both landed in ``cap_hit`` through the bare ``"budget" in terminal`` test
    and in neither of the two columns that split it, so a run stopped by either
    was counted once and attributed nowhere.
    """
    cell = tmp_path / "team-spent"
    runtime = _runtime_dir(cell, "team", "django__django-13933")
    _seat_file(runtime, "agent_0_analyst-aa.json", aid=0, role="analyst",
               tokens=2_000_000, assistant=40, terminal=SPENT_TERMINAL)
    _seat_file(runtime, "agent_1_coder-bb.json", aid=1, role="coder",
               tokens=10, assistant=1,
               terminal="team budget exceeded: aggregate spend reached the global cap")
    _write_metrics(cell, [{
        "instance_id": "django__django-13933",
        "run_summary": {"status": "stopped", "reason": SPENT_TERMINAL,
                        "tokens": 2_000_010, "steps": 41},
    }])

    rows = cell_report.run_rows(cell, "team")

    assert rows[0].cap_hit == ["0", "1"]
    assert rows[0].cap_hit_precheck == ["0"]
    assert rows[0].cap_hit_aggregate == ["1"]


def test_a_cell_with_no_event_log_still_reads_and_says_the_cap_is_unknown(
    dw_cell: Path,
) -> None:
    """The mutation control: the ceiling is an addition, never a precondition.

    The ``dw_cell`` fixture writes seat files and no event log at all, which is
    every batch pulled before this column existed. Those runs must keep every
    reading they had and report the allowance as unknown, not as zero -- zero
    would make every one of them look fully spent.
    """
    rows = _rows(dw_cell)

    assert rows["django__django-12262"].seat_cap is None
    assert rows["django__django-12262"].seat_headroom_min is None
    assert rows["django__django-12262"].role_headroom == {}
    assert rows["django__django-12262"].seats["0"].cap is None
    # Everything the old report said about this cell is unchanged.
    assert rows["django__django-12262"].delivered is True
    assert rows["astropy__astropy-12907"].cap_hit_precheck == ["0"]


def test_an_arm_with_no_seat_cap_field_gets_its_allowance_from_the_event_log(
    tmp_path: Path,
) -> None:
    """The control M2a needs: the arms where the log is the only source.

    Only the scripted workflow writes ``workflow_result.seat_cap``. A team or
    single run's allowance exists nowhere but its ``session_terminal`` events,
    so if the log goes unread those two arms lose the column entirely rather
    than falling back to anything.
    """
    cell = tmp_path / "team-headroom"
    runtime = _runtime_dir(cell, "team", "astropy__astropy-14369")
    _seat_file(runtime, "agent_0_analyst-aa.json", aid=0, role="analyst",
               tokens=1_981_974, assistant=57, terminal=PRECHECK_TERMINAL)
    _seat_file(runtime, "agent_1_coder-bb.json", aid=1, role="coder",
               tokens=0, assistant=0)
    _event_log(runtime, "trajectory.jsonl", [
        {"aid": 0, "used_tokens": 1_981_974, "max_budget_tokens": 2_000_000,
         "terminal_reason": PRECHECK_TERMINAL},
    ])
    _write_metrics(cell, [{
        "instance_id": "astropy__astropy-14369",
        "run_summary": {"status": "stopped", "reason": PRECHECK_TERMINAL,
                        "tokens": 1_981_974, "steps": 57},
    }])

    rows = cell_report.run_rows(cell, "team")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_cap == 2_000_000
    assert rows[0].role_headroom == {"analyst": 18_026, "coder": 2_000_000}
    assert rows[0].seat_headroom_min == 18_026
    assert summary["seat_cap_tokens"] == [2_000_000]
    assert summary["seat_cap_unknown"] == []
    assert summary["seat_headroom_min"] == [["astropy__astropy-14369", 18_026]]
    assert "18,026" in cell_report.render(rows, summary, [])
