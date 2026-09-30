"""Offline behavior checks for research batches and cell reports."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.batch_spec import SpecError, load_spec
from tests.experiment.cell_report_support import (
    POSTCALL_TERMINAL as POSTCALL_TERMINAL,
)
from tests.experiment.cell_report_support import (
    PRECHECK_TERMINAL as PRECHECK_TERMINAL,
)
from tests.experiment.cell_report_support import (
    SIX_EDGES as SIX_EDGES,
)
from tests.experiment.cell_report_support import (
    SPENT_TERMINAL as SPENT_TERMINAL,
)
from tests.experiment.cell_report_support import (
    STRUCTURED_CAPTURE_TERMINAL as STRUCTURED_CAPTURE_TERMINAL,
)
from tests.experiment.cell_report_support import (
    _event_log as _event_log,
)
from tests.experiment.cell_report_support import (
    _messages as _messages,
)
from tests.experiment.cell_report_support import (
    _rows as _rows,
)
from tests.experiment.cell_report_support import (
    _runtime_dir as _runtime_dir,
)
from tests.experiment.cell_report_support import (
    _seat_file as _seat_file,
)
from tests.experiment.cell_report_support import (
    _seat_with_messages as _seat_with_messages,
)
from tests.experiment.cell_report_support import (
    _single_cell as _single_cell,
)
from tests.experiment.cell_report_support import (
    _timeout_cell as _timeout_cell,
)
from tests.experiment.cell_report_support import (
    _topology_log as _topology_log,
)
from tests.experiment.cell_report_support import (
    _two_candidate_cell as _two_candidate_cell,
)
from tests.experiment.cell_report_support import (
    _write_metrics as _write_metrics,
)
from tests.experiment.cell_report_support import (
    dw_capped_cell as dw_capped_cell,
)
from tests.experiment.cell_report_support import (
    dw_cell as dw_cell,
)
from tests.experiment.cell_report_support import (
    dw_edges_cell as dw_edges_cell,
)
from tests.experiment.cell_report_support import (
    team_edges_cell as team_edges_cell,
)


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
    _seat_file(
        runtime,
        "agent_0_analyst-f44ceb062e35.json",
        aid=0,
        role="analyst",
        tokens=500_000,
        assistant=20,
        tool_name="message_agent",
    )
    _seat_file(
        runtime,
        "agent_1_coder-c84a9e3ad144.json",
        aid=1,
        role="coder",
        tokens=300_000,
        assistant=12,
        tool_name="apply_patch",
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "astropy__astropy-14096",
                "run_summary": {"status": "completed", "tokens": 800_000, "steps": 32},
                "tree_snapshots": [{"after": "analyst"}],
            }
        ],
    )

    rows = {r.instance_id: r for r in cell_report.run_rows(cell, "team")}
    seats = rows["astropy__astropy-14096"].seats

    assert {s.role for s in seats.values()} == {"analyst", "coder"}
    assert seats["0"].msg_agent == 20
    assert seats["1"].writes == 12
    assert rows["astropy__astropy-14096"].delivered is True
    assert rows["astropy__astropy-14096"].tree_snapshots == 1


def test_a_workflow_seat_takes_its_role_from_its_file_name(dw_cell: Path) -> None:
    """The runtime stamps every workflow seat ``workflow_agent``.

    Finding the files is not enough: with one generic role for all of them,
    ``delivered`` -- a coder or tester seat that spent tokens and spoke -- is
    false on every run whatever the run did.
    """
    seats = _rows(dw_cell)["django__django-12262"].seats

    assert [seats[str(i)].role for i in range(4)] == [
        "analyst",
        "coder",
        "tester",
        "analyst",
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


def test_tree_snapshots_fall_back_to_the_workflow_result(dw_cell: Path) -> None:
    """A workflow writes its boundaries under ``workflow_result``, not at the top.

    Read only at the top level a DW run shows zero -- indistinguishable from an
    arm that records no boundaries at all.
    """
    rows = _rows(dw_cell)

    assert rows["django__django-12262"].tree_snapshots == 2
    assert rows["astropy__astropy-12907"].tree_snapshots == 1


def test_the_analyst_writing_the_source_reaches_the_cell_report(dw_cell: Path) -> None:
    """The probe between analyze and implement stopped at ``metrics.jsonl``.

    In dw-subset50-r2 forty-nine of fifty runs recorded it true: the analyst
    had already changed the source when the script went to call the coder. The
    arm reads as a three-role workflow and ran as one agent fixing the task
    with two more reviewing it. A number that has to be dug out of the raw
    metrics by hand is a number the paper does not have.
    """
    rows = _rows(dw_cell)

    assert rows["django__django-12262"].analyst_wrote_source is True
    assert rows["astropy__astropy-12907"].analyst_wrote_source is False


def test_a_cell_that_never_probed_the_tree_is_not_summarised_as_a_clean_zero(
    dw_cell: Path,
) -> None:
    """Zero runs that wrote and zero runs that were asked are the same count.

    The summary carries both, so "the analyst never wrote" cannot be read off a
    cell where nothing ever looked.
    """
    rows = list(_rows(dw_cell).values())
    summary = cell_report.summarize(rows, [])

    assert summary["analyst_wrote_source"] == 1
    assert summary["analyst_wrote_source_probed"] == 2

    blind = [replace(r, analyst_wrote_source=None) for r in rows]
    blind_summary = cell_report.summarize(blind, [])
    assert blind_summary["analyst_wrote_source"] == 0
    assert blind_summary["analyst_wrote_source_probed"] == 0


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

    summary = cell_report.summarize(cell_report.run_rows(dw_cell, "self-collaboration"), None, team=True)
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
    path.write_text(body.replace("suite: tiny\n", "cell: facts-v2\nsuite: tiny\n"), encoding="utf-8")
    with pytest.raises(SpecError, match="takes no 'cell'"):
        load_spec(path)


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
    _seat_file(
        seat_dir, "agent.json", aid=-1, role="swe_agent", tokens=2_000_512, assistant=40, terminal=POSTCALL_TERMINAL
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-13933",
                "trajectory_path": f"/home/someone/oc-team-smoke/x/{seat_dir.name}",
                "run_summary": {"status": "stopped", "reason": POSTCALL_TERMINAL, "tokens": 2_000_512, "steps": 40},
            }
        ],
    )

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
    _seat_file(
        cell / "agent-deadbeef",
        "agent.json",
        aid=-1,
        role="swe_agent",
        tokens=999,
        assistant=3,
        terminal=PRECHECK_TERMINAL,
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-13933",
                "run_summary": {"status": "completed", "tokens": 999, "steps": 3},
            }
        ],
    )

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
    _seat_file(found_dir, "agent.json", aid=-1, role="swe_agent", tokens=378_313, assistant=21, terminal="submitted")
    _write_metrics(
        cell,
        [
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
        ],
    )

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
    _write_metrics(
        cell,
        [
            {
                "instance_id": "sympy__sympy-24213",
                "run_summary": {"status": "completed", "tokens": 511_004, "steps": 30},
            }
        ],
    )
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
    _write_metrics(
        dw_cell,
        [
            {
                "instance_id": "django__django-12262",
                "run_summary": {"status": "completed", "tokens": 452_053, "steps": 43},
            },
            {
                "instance_id": "pydata__xarray-4075",
                "run_summary": {"status": "stopped", "reason": "container gone", "tokens": 0, "steps": 0},
            },
        ],
    )

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
        ".opencollab",
        "best-of-n",
        "django__django-13933",
        "candidate-1",
    )
    assert "best-of-n" not in cell_report.SEAT_AT_BATCH_ROOT_ARMS
    assert cell_report.SEAT_AT_BATCH_ROOT_ARMS == frozenset({"single"})


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
    line = next(row for row in text.splitlines() if "django__django-12262" in row)

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
    _seat_file(runtime, "000_analyst.json", aid=0, role="workflow_agent", tokens=209_224, assistant=16)
    _seat_file(runtime, "003_analyst-adjudicate-r1.json", aid=3, role="workflow_agent", tokens=38_137, assistant=5)
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-12262",
                "run_summary": {"status": "completed", "tokens": 247_361, "steps": 21},
                "workflow_result": {"status": "done", "seat_cap": 2_000_000, "seat_spend": {"analyst": 247_361}},
            }
        ],
    )

    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_spend_recorded == {"analyst": 247_361}
    assert rows[0].seat_spend_agrees is True
    assert summary["seat_spend_disagrees"] == []

    # Mutation control: break the ledger and the disagreement must surface.
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-12262",
                "run_summary": {"status": "completed", "tokens": 247_361, "steps": 21},
                "workflow_result": {"status": "done", "seat_cap": 2_000_000, "seat_spend": {"analyst": 38_137}},
            }
        ],
    )
    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_spend_agrees is False
    assert summary["seat_spend_disagrees"] == ["django__django-12262"]


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
