"""Offline behavior checks for research batches and cell reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.experiment import cell_report
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
    _seat_file(
        runtime,
        "agent_0_analyst-aa.json",
        aid=0,
        role="analyst",
        tokens=1_981_974,
        assistant=57,
        terminal=PRECHECK_TERMINAL,
    )
    _seat_file(runtime, "agent_1_coder-bb.json", aid=1, role="coder", tokens=0, assistant=0)
    _event_log(
        runtime,
        "trajectory.jsonl",
        [
            {"aid": 0, "used_tokens": 1_981_974, "max_budget_tokens": 2_000_000, "terminal_reason": PRECHECK_TERMINAL},
        ],
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "astropy__astropy-14369",
                "run_summary": {"status": "stopped", "reason": PRECHECK_TERMINAL, "tokens": 1_981_974, "steps": 57},
            }
        ],
    )

    rows = cell_report.run_rows(cell, "team")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].seat_cap == 2_000_000
    assert rows[0].role_headroom == {"analyst": 18_026, "coder": 2_000_000}
    assert rows[0].seat_headroom_min == 18_026
    assert summary["seat_cap_tokens"] == [2_000_000]
    assert summary["seat_cap_unknown"] == []
    assert summary["seat_headroom_min"] == [["astropy__astropy-14369", 18_026]]
    assert "18,026" in cell_report.render(rows, summary, [])


def test_the_scripted_arm_reports_no_alpha(dw_edges_cell: Path) -> None:
    rows = cell_report.run_rows(dw_edges_cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True, alpha_readable=False)

    assert summary["alpha_readable"] is False
    assert summary["alpha"] is None
    assert summary["delivered"] is None
    assert summary["ci95"] is None
    # The seat columns the arm does have are untouched.
    assert summary["valid"] == 3
    assert summary["tokens_total"] == 780_000


def test_the_scripted_arm_reports_the_edges_it_walked(dw_edges_cell: Path) -> None:
    rows = cell_report.run_rows(dw_edges_cell, "self-collaboration")
    by_id = {r.instance_id: r for r in rows}
    summary = cell_report.summarize(rows, None, team=True, alpha_readable=False)

    assert by_id["astropy__astropy-14369"].edges_walked == 6
    assert by_id["astropy__astropy-14369"].edges_declared == 6
    assert by_id["django__django-13933"].edges_walked == 5
    assert by_id["matplotlib__matplotlib-25775"].edges_walked == 0
    assert summary["edges_walked"] == 11
    assert summary["edges_declared"] == 18
    assert summary["edges_walked_rate"] == pytest.approx(11 / 18)
    assert summary["edges_unwalked"] == [
        ["django__django-13933", 5, 6],
        ["matplotlib__matplotlib-25775", 0, 6],
    ]


def test_the_scripted_arm_s_report_prints_edges_where_alpha_was(
    dw_edges_cell: Path,
) -> None:
    rows = cell_report.run_rows(dw_edges_cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True, alpha_readable=False)
    text = cell_report.render(rows, summary, [])

    assert "edges walked 11/18" in text
    assert "Clopper-Pearson" not in text
    assert "delivered" not in text
    # And the run that walked none of its six is named, not just averaged in.
    assert "matplotlib__matplotlib-25775" in text.split("edges walked", 1)[1]


def test_a_team_cell_still_reports_alpha(dw_edges_cell: Path) -> None:
    """The mutation control: the change is which arms, not the quantity."""
    rows = cell_report.run_rows(dw_edges_cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)
    text = cell_report.render(rows, summary, [])

    assert summary["alpha_readable"] is True
    assert summary["alpha"] == pytest.approx(2 / 3)
    assert summary["ci95"] is not None
    assert "Clopper-Pearson" in text


def test_the_alpha_readable_set_is_the_team_arms_and_nothing_else() -> None:
    """Where the choice is made, so widening it again is a visible edit.

    ``DELIVERY_READABLE_ARMS`` stays as it is -- a DW run does have seats worth
    counting, which is what that set is for -- and the narrower question "did
    an agent choose to hand work on" gets its own set.
    """
    from opencollab_eval.generation.gen_prediction_batch import (
        DELIVERY_READABLE_ARMS,
        TEAM_ARMS,
    )

    assert cell_report.ALPHA_READABLE_ARMS == TEAM_ARMS
    assert "self-collaboration" in DELIVERY_READABLE_ARMS
    assert "self-collaboration" not in cell_report.ALPHA_READABLE_ARMS
    assert "single" not in cell_report.ALPHA_READABLE_ARMS


def test_a_timeout_is_read_off_the_block_every_arm_writes(tmp_path: Path) -> None:
    rows = cell_report.run_rows(_timeout_cell(tmp_path), "team")
    by_id = {r.instance_id: r for r in rows}

    assert by_id["matplotlib__matplotlib-24870"].timeout_censored is True
    assert by_id["matplotlib__matplotlib-24870"].duration_s == pytest.approx(5401.108, abs=1e-3)
    assert by_id["django__django-13933"].timeout_censored is False
    # A run stopped by its budget is stopped, not censored.
    assert by_id["sympy__sympy-15875"].timeout_censored is False


def test_the_summary_counts_censored_runs_and_names_them(tmp_path: Path) -> None:
    rows = cell_report.run_rows(_timeout_cell(tmp_path), "team")
    summary = cell_report.summarize(rows, None, team=True, timeout_s=5400)

    assert summary["timeout_censored"] == ["matplotlib__matplotlib-24870"]
    assert summary["timeout_censored_count"] == 1
    assert summary["timeout_s"] == 5400
    # The independent derived check: duration against the wall the spec set.
    assert summary["duration_at_or_over_timeout"] == ["matplotlib__matplotlib-24870"]
    assert summary["timeout_rule_disagreement"] == []
    assert summary["duration_max_s"] == pytest.approx(5401.108, abs=1e-3)


def test_the_two_timeout_readings_are_reported_when_they_disagree(
    tmp_path: Path,
) -> None:
    """The recorded reason and the clock have to be able to contradict.

    A run that ran past the wall without the reason, or carries the reason
    having stopped well inside it, means one of the two is not measuring what
    it is read as -- which is the whole reason this column is derived on the
    two arms that do not write one.
    """
    cell = tmp_path / "team-disagree"
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-13933",
                "run_summary": {"status": "completed", "reason": None, "tokens": 10, "steps": 1, "duration_s": 5600.0},
            }
        ],
    )
    rows = cell_report.run_rows(cell, "team")
    summary = cell_report.summarize(rows, None, team=True, timeout_s=5400)

    assert summary["timeout_censored"] == []
    assert summary["duration_at_or_over_timeout"] == ["django__django-13933"]
    assert summary["timeout_rule_disagreement"] == ["django__django-13933"]
    assert "timeout readings disagree" in cell_report.render(rows, summary, [])


def test_the_censored_count_is_printed_on_every_arm(tmp_path: Path) -> None:
    """A JSON key nobody prints is not a column, and all three arms need it."""
    rows = cell_report.run_rows(_timeout_cell(tmp_path), "team")
    team_text = cell_report.render(rows, cell_report.summarize(rows, None, team=True, timeout_s=5400), [])
    single_text = cell_report.render(rows, cell_report.summarize(rows, None, team=False, timeout_s=5400), [])

    for text in (team_text, single_text):
        assert "cut off at the wall clock: 1/3" in text
        assert "matplotlib__matplotlib-24870" in text.split("cut off at the wall clock", 1)[1]
        # The wall itself, so the count can be checked against the spec.
        assert "5400" in text


def test_the_single_arm_s_own_flag_agrees_with_the_derived_one(tmp_path: Path) -> None:
    """Positive control on the arm that does write an indicator.

    ``wall_clock_timeout`` is the driver's own answer for the single arm. The
    derived rule has to reproduce it, or it is not the same quantity being
    extended to the other two arms.
    """
    cell = tmp_path / "single-timeout"
    seat_dir = cell / "agent-9c1f"
    _seat_file(seat_dir, "agent.json", aid=-1, role="swe_agent", tokens=900_000, assistant=30, terminal="")
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-13933",
                "trajectory_path": f"/home/someone/single/{seat_dir.name}",
                "wall_clock_timeout": True,
                "run_summary": {
                    "status": "stopped",
                    "reason": "timeout",
                    "tokens": 900_000,
                    "steps": 30,
                    "duration_s": 5402.0,
                },
            }
        ],
    )

    rows = cell_report.run_rows(cell, "single")

    assert rows[0].timeout_censored is True
    assert rows[0].timeout_recorded is True
    assert cell_report.summarize(rows, None, team=False, timeout_s=5400)["timeout_flag_disagreement"] == []


def test_alpha_reads_the_delegate_seats_off_the_run_not_off_two_role_names(tmp_path: Path) -> None:
    """A roster of an Adopter and two Coders delivers, and the old reader said it did not.

    The literal pair ``{"coder", "tester"}`` is the handoff team's answer. On
    this team it matches no seat, so every run reads as delivering nothing --
    alpha 0.000 with a Clopper-Pearson interval printed around it, which is
    what makes it worth a test: the wrong number arrives looking like a number.
    """
    cell = tmp_path / "dual"
    runtime = _runtime_dir(cell, "team", "a")
    _seat_with_messages(
        runtime,
        "agent_0_adopter-aa.json",
        aid=0,
        role="adopter",
        targets=[{"to_role": "coder_a", "summary": "s", "content": "c"}],
    )
    _seat_file(runtime, "agent_1_coder_a-bb.json", aid=1, role="coder_a", tokens=5_000, assistant=2)
    _seat_file(runtime, "agent_2_coder_b-cc.json", aid=2, role="coder_b", tokens=0, assistant=0)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "trajectory.jsonl").write_text(
        json.dumps(
            {
                "type": "assigned.topology_nodes",
                "payload": {
                    "entry_role": "adopter",
                    "declared_roles": ["adopter", "coder_a", "coder_b"],
                    "nodes": [
                        {"aid": 0, "role": "adopter", "entry": True},
                        {"aid": 1, "role": "coder_a", "entry": False},
                        {"aid": 2, "role": "coder_b", "entry": False},
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_metrics(cell, [{"instance_id": "a", "run_summary": {"status": "completed", "tokens": 20_000, "steps": 4}}])

    rows = cell_report.run_rows(cell, "team")
    assert [r.delivered for r in rows] == [True]
    assert cell_report.summarize(rows, expected_card=None)["alpha"] == 1.0
    # The seats the run itself names, and the pair that would have been used.
    assert cell_report.DELEGATE_ROLES.isdisjoint({"adopter", "coder_a", "coder_b"})


def test_one_candidate_delivers_but_is_not_every_delegate(tmp_path: Path) -> None:
    """The failure alpha cannot see on this roster.

    The card asks the Adopter for two candidates: brief one Coder, and when its
    answer is back write the second its own brief. A run that used one Coder
    and never addressed the other produced ONE candidate -- which is the thing
    this family exists to measure -- and alpha reads it as adherence, because a
    delegate seat did spend tokens and did speak.
    """
    rows = cell_report.run_rows(_two_candidate_cell(tmp_path, coder_b_worked=False), "team")

    assert [r.delivered for r in rows] == [True]
    assert [r.every_delegate for r in rows] == [False]

    summary = cell_report.summarize(rows, expected_card=None)
    assert summary["alpha"] == 1.0
    assert summary["every_delegate"] == 0
    assert summary["every_delegate_rate"] == 0.0
    assert all(bound is not None for bound in summary["every_delegate_ci95"])


def test_both_candidates_make_the_second_rate_agree_with_alpha(tmp_path: Path) -> None:
    rows = cell_report.run_rows(_two_candidate_cell(tmp_path, coder_b_worked=True), "team")

    assert [r.every_delegate for r in rows] == [True]
    summary = cell_report.summarize(rows, expected_card=None)
    assert summary["every_delegate_rate"] == summary["alpha"] == 1.0


def test_the_second_rate_is_printed_only_when_it_differs_from_alpha(tmp_path: Path) -> None:
    """Saying the same number twice reads as two findings."""
    one = cell_report.run_rows(_two_candidate_cell(tmp_path / "one", coder_b_worked=False), "team")
    both = cell_report.run_rows(_two_candidate_cell(tmp_path / "both", coder_b_worked=True), "team")

    printed = cell_report.render(one, cell_report.summarize(one, None), [])
    assert "every delegate seat 0/1" in printed
    printed = cell_report.render(both, cell_report.summarize(both, None), [])
    assert "every delegate seat" not in printed


def test_a_team_run_declares_its_edges_in_its_trajectory(team_edges_cell: Path) -> None:
    by_id = {r.instance_id: r for r in cell_report.run_rows(team_edges_cell, "team")}
    assert by_id["a"].edges_declared == 6
    assert by_id["b"].edges_declared == 6
    assert by_id["c"].edges_declared == 6


def test_a_team_edge_is_walked_by_a_message_over_it(team_edges_cell: Path) -> None:
    by_id = {r.instance_id: r for r in cell_report.run_rows(team_edges_cell, "team")}
    # analyst -> coder, addressed by role.
    assert by_id["a"].edges_walked == 1
    # analyst -> tester by aid and analyst -> coder by role: two distinct
    # edges. The third message addresses a role the team file never declared an
    # edge to, and does not become a third.
    assert by_id["b"].edges_walked == 2
    # Seated, spoke, never addressed a teammate.
    assert by_id["c"].edges_walked == 0


def test_the_team_summary_carries_the_edge_counts_beside_alpha(team_edges_cell: Path) -> None:
    rows = cell_report.run_rows(team_edges_cell, "team")
    summary = cell_report.summarize(rows, None, team=True, alpha_readable=True)
    assert summary["edges_declared"] == 18
    assert summary["edges_walked"] == 3
    assert summary["edges_walked_rate"] == pytest.approx(3 / 18)
    assert summary["edges_declared_state"] == "declared"
    # Alpha is still alpha: it is not replaced by the edge count.
    assert summary["alpha_readable"] is True and summary["alpha"] is not None
    text = cell_report.render(rows, summary, [])
    assert "Clopper-Pearson" in text
    assert "edges walked 3/18" in text


def test_an_open_topology_declares_no_edges(tmp_path: Path) -> None:
    """``allow_all`` means every pair is permitted, so there is no declared set.

    An empty ``edges`` list under ``allow_all`` would otherwise read as
    "nobody may talk", which is its opposite.
    """
    cell = tmp_path / "open"
    runtime = _runtime_dir(cell, "team", "a")
    _seat_with_messages(
        runtime,
        "agent_0_analyst-aa.json",
        aid=0,
        role="analyst",
        targets=[{"to_role": "coder", "summary": "s", "content": "c"}],
    )
    _seat_file(runtime, "agent_1_coder-bb.json", aid=1, role="coder", tokens=5_000, assistant=2)
    _topology_log(runtime, [], allow_all=True)
    _write_metrics(cell, [{"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}}])
    rows = cell_report.run_rows(cell, "team")
    assert rows[0].edges_declared is None and rows[0].edges_walked is None
    # And the control that separates "open" from "closed and empty": a team
    # file that declares no edge at all is a declaration of zero.
    assert cell_report.declared_edges({"allow_all": False, "edges": []}) == set()
    assert cell_report.declared_edges({"allow_all": True, "edges": []}) is None
    summary = cell_report.summarize(rows, None, team=True)
    assert summary["edges_declared_state"] == "not_declared"
    assert summary["edges_walked_rate"] is None


def test_an_arm_that_declares_no_topology_says_so(tmp_path: Path) -> None:
    cell = tmp_path / "single"
    _write_metrics(cell, [{"instance_id": "a", "run_summary": {"status": "completed", "tokens": 10}}])
    rows = cell_report.run_rows(cell, "single")
    summary = cell_report.summarize(rows, None, team=False)
    assert summary["edges_declared_state"] == "not_declared"
    assert summary["edges_declared"] == 0 and summary["edges_walked_rate"] is None
    assert "edges declared: none" in cell_report.render(rows, summary, [])

def test_the_workflow_s_own_budget_exhausted_verdict_is_read(tmp_path: Path) -> None:
    """``self_collaboration.py:466-467`` decides a seat is spent and records it.

    ``workflow_result.status == "budget_exhausted"`` is the workflow's own
    finding, written when ``exhausted(seat)`` stops a round. Nothing read it,
    so an arm whose script stopped for want of budget reported the same status
    vocabulary as one that finished.
    """
    cell = tmp_path / "dw-exhausted"
    runtime = _runtime_dir(cell, "self-collaboration", "sympy__sympy-15875")
    _seat_file(
        runtime,
        "000_analyst.json",
        aid=0,
        role="workflow_agent",
        tokens=2_000_000,
        assistant=60,
        terminal=STRUCTURED_CAPTURE_TERMINAL,
    )
    _event_log(
        runtime,
        "orchestration.jsonl",
        [
            {
                "aid": 0,
                "used_tokens": 2_000_000,
                "max_budget_tokens": 2_000_000,
                "terminal_reason": STRUCTURED_CAPTURE_TERMINAL,
            },
        ],
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "sympy__sympy-15875",
                "run_summary": {"status": "completed", "tokens": 2_000_000, "steps": 60},
                "workflow_result": {
                    "status": "budget_exhausted",
                    "seat_cap": 2_000_000,
                    "seat_spend": {"analyst": 2_000_000},
                },
            }
        ],
    )

    rows = cell_report.run_rows(cell, "self-collaboration")
    summary = cell_report.summarize(rows, None, team=True)

    assert rows[0].budget_exhausted is True
    assert rows[0].seat_headroom_min == 0
    assert summary["seat_budget_exhausted"] == ["sympy__sympy-15875"]
    assert "seat budget exhausted (the workflow's own verdict): 1" in cell_report.render(rows, summary, [])


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
    _seat_file(
        runtime,
        "agent_0_analyst-aa.json",
        aid=0,
        role="analyst",
        tokens=2_000_000,
        assistant=40,
        terminal=SPENT_TERMINAL,
    )
    _seat_file(
        runtime,
        "agent_1_coder-bb.json",
        aid=1,
        role="coder",
        tokens=10,
        assistant=1,
        terminal="team budget exceeded: aggregate spend reached the global cap",
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-13933",
                "run_summary": {"status": "stopped", "reason": SPENT_TERMINAL, "tokens": 2_000_010, "steps": 41},
            }
        ],
    )

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
