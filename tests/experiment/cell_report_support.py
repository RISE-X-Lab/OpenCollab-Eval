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

PRECHECK_TERMINAL = (
    "budget exhausted before model call: conservative input reservation requires 118192 of 101794 remaining tokens"
)
POSTCALL_TERMINAL = "budget exceeded after model call: 2000512 tokens used"


def _messages(assistant_turns: int, tool_name: str | None = None) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "fix it"}]
    for i in range(assistant_turns):
        message: dict = {"role": "assistant", "content": f"turn {i}"}
        if tool_name:
            message["tool_calls"] = [{"id": f"c{i}", "function": {"name": tool_name, "arguments": "{}"}}]
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
    return cell / f"logs-{arm}" / instance_id / "trajectories" / "solver-aa" / "runtime-bb"


def _write_metrics(cell: Path, records: list[dict]) -> None:
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def dw_cell(tmp_path: Path) -> Path:
    """One healthy DW run and one that stopped with no brief, as the driver writes them."""
    cell = tmp_path / "dw-cell"

    healthy = _runtime_dir(cell, "self-collaboration", "django__django-12262")
    _seat_file(
        healthy, "000_analyst.json", aid=0, role="workflow_agent", tokens=209_224, assistant=16, tool_name="file_write"
    )
    _seat_file(
        healthy, "001_coder-r1.json", aid=1, role="workflow_agent", tokens=93_604, assistant=11, tool_name="apply_patch"
    )
    _seat_file(healthy, "002_tester-r1.json", aid=2, role="workflow_agent", tokens=111_088, assistant=11)
    _seat_file(healthy, "003_analyst-adjudicate-r1.json", aid=3, role="workflow_agent", tokens=38_137, assistant=5)

    degenerate = _runtime_dir(cell, "self-collaboration", "astropy__astropy-12907")
    _seat_file(
        degenerate,
        "000_analyst.json",
        aid=0,
        role="workflow_agent",
        tokens=1_898_206,
        assistant=61,
        terminal=PRECHECK_TERMINAL,
    )
    _seat_file(
        degenerate, "001_analyst.json", aid=1, role="workflow_agent", tokens=0, assistant=0, terminal=POSTCALL_TERMINAL
    )

    _write_metrics(
        cell,
        [
            {
                "instance_id": "django__django-12262",
                "run_summary": {"status": "completed", "reason": None, "tokens": 452_053, "steps": 43},
                "workflow_result": {
                    "status": "done",
                    "analyst_wrote_source": True,
                    "tree_snapshots": [{"after": "analyze"}, {"after": "implement:r1"}],
                },
                "submitted_patch_chars": 812,
            },
            {
                "instance_id": "astropy__astropy-12907",
                "run_summary": {
                    "status": "stopped",
                    "reason": "analyst produced no structured brief",
                    "tokens": 1_898_206,
                    "steps": 61,
                },
                "workflow_result": {
                    "status": "error",
                    "error": "analyst produced no structured brief",
                    "analyst_wrote_source": False,
                    "tree_snapshots": [{"after": "analyze"}],
                },
                "submitted_patch_chars": 0,
            },
        ],
    )
    return cell


def _rows(cell: Path) -> dict[str, cell_report.RunRow]:
    rows = cell_report.run_rows(cell, "self-collaboration")
    return {r.instance_id: r for r in rows}


# --- E3: the seat files are found at all ------------------------------------ #


# --- E4: the seat's role, and therefore delivery ---------------------------- #


# --- E5: the snapshots a workflow records inside its own result ------------- #


# --- E7: the two things spelt "budget" ------------------------------------- #


# --- E6: delivery is read on the DW arm without widening TEAM_ARMS ---------- #


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
    _seat_file(
        capped_dir, "agent.json", aid=-1, role="swe_agent", tokens=1_968_907, assistant=53, terminal=PRECHECK_TERMINAL
    )
    _seat_file(clean_dir, "agent.json", aid=-1, role="swe_agent", tokens=378_313, assistant=21, terminal="submitted")
    for instance in ("matplotlib__matplotlib-25775", "django__django-13933"):
        (cell / "logs-single" / instance).mkdir(parents=True, exist_ok=True)
        (cell / "logs-single" / instance / "driver.log").write_text("", encoding="utf-8")
    _write_metrics(
        cell,
        [
            {
                "instance_id": "matplotlib__matplotlib-25775",
                "trajectory_path": f"/home/someone/oc-team-smoke/single/{capped_dir.name}",
                "run_summary": {"status": "stopped", "reason": PRECHECK_TERMINAL, "tokens": 1_968_907, "steps": 53},
                "submitted_patch_chars": 3231,
            },
            {
                "instance_id": "django__django-13933",
                "trajectory_path": f"/home/someone/oc-team-smoke/single/{clean_dir.name}",
                "run_summary": {"status": "completed", "reason": None, "tokens": 378_313, "steps": 21},
                "submitted_patch_chars": 792,
            },
        ],
    )
    return cell


# --- E9: a seat file that was never found, said out loud -------------------- #


# --- E10: a role seated twice in one run ------------------------------------ #
#
# The scripted workflow seats its analyst twice -- ``000_analyst`` to write the
# brief and ``003_analyst-adjudicate-r1`` to rule on the tester's report -- and
# the report keyed the render's per-role lookup by role alone, so the second
# seat replaced the first and the analyst column printed the adjudication's
# spend as if it were the whole role's. On the smoke DW batch that printed
# 48,695 where the run spent 1,995,025.


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
    (directory / filename).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


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
    _seat_file(
        runtime,
        "000_analyst.json",
        aid=0,
        role="workflow_agent",
        tokens=1_946_330,
        assistant=62,
        terminal=STRUCTURED_CAPTURE_TERMINAL,
    )
    _seat_file(
        runtime,
        "001_coder-r1.json",
        aid=1,
        role="workflow_agent",
        tokens=63_791,
        assistant=6,
        terminal=STRUCTURED_CAPTURE_TERMINAL,
    )
    _seat_file(
        runtime,
        "002_tester-r1.json",
        aid=2,
        role="workflow_agent",
        tokens=210_509,
        assistant=16,
        terminal=STRUCTURED_CAPTURE_TERMINAL,
    )
    _seat_file(
        runtime,
        "003_analyst-adjudicate-r1.json",
        aid=3,
        role="workflow_agent",
        tokens=48_695,
        assistant=4,
        terminal=STRUCTURED_CAPTURE_TERMINAL,
    )
    _event_log(
        runtime,
        "orchestration.jsonl",
        [
            {
                "aid": 0,
                "used_tokens": 1_946_330,
                "max_budget_tokens": 2_000_000,
                "terminal_reason": STRUCTURED_CAPTURE_TERMINAL,
            },
            {
                "aid": 1,
                "used_tokens": 63_791,
                "max_budget_tokens": 2_000_000,
                "terminal_reason": STRUCTURED_CAPTURE_TERMINAL,
            },
            {
                "aid": 2,
                "used_tokens": 210_509,
                "max_budget_tokens": 2_000_000,
                "terminal_reason": STRUCTURED_CAPTURE_TERMINAL,
            },
            {
                "aid": 3,
                "used_tokens": 48_695,
                "max_budget_tokens": 53_670,
                "terminal_reason": STRUCTURED_CAPTURE_TERMINAL,
            },
        ],
    )
    _write_metrics(
        cell,
        [
            {
                "instance_id": "astropy__astropy-14369",
                "run_summary": {
                    "status": "completed",
                    "reason": None,
                    "tokens": 2_269_325,
                    "steps": 88,
                    "duration_s": 1265.15,
                },
                "workflow_result": {
                    "status": "done",
                    "seat_cap": 2_000_000,
                    "seat_spend": {"analyst": 1_995_025, "coder": 63_791, "tester": 210_509},
                    "tree_snapshots": [{"after": "analyze"}, {"after": "implement:r1"}],
                },
            }
        ],
    )
    return cell


# --- E12: the arm whose edges are written by a script ----------------------- #
#
# ``batch.py`` chose the delivery reading by ``arm in DELIVERY_READABLE_ARMS``,
# which holds the scripted workflow, so a DW cell reported
# ``alpha = 0.750 (0.194, 0.994)``. That number is not the paper's alpha. Alpha
# is the rate at which an agent *chose* to hand work on; the workflow's edges
# are sequenced by ``self_collaboration.py``, so what its runs vary is whether
# a scripted edge carried anything -- recorded, per run, as ``edges_walked``
# against ``edges_declared``. Reporting a choice rate for an arm that makes no
# choice put a confidence interval on the wrong quantity.


@pytest.fixture
def dw_edges_cell(tmp_path: Path) -> Path:
    """Three DW runs at 6/6, 5/6 and 0/6 declared edges, as the smoke batch ran."""
    cell = tmp_path / "dw-edges"
    declared = [
        "analyst->coder",
        "analyst->tester",
        "coder->tester",
        "coder->analyst",
        "tester->coder",
        "tester->analyst",
    ]
    records = []
    for instance, walked in (
        ("astropy__astropy-14369", declared),
        ("django__django-13933", declared[:4] + ["tester->analyst"]),
        ("matplotlib__matplotlib-25775", []),
    ):
        runtime = _runtime_dir(cell, "self-collaboration", instance)
        _seat_file(runtime, "000_analyst.json", aid=0, role="workflow_agent", tokens=200_000, assistant=10)
        if walked:
            _seat_file(runtime, "001_coder-r1.json", aid=1, role="workflow_agent", tokens=60_000, assistant=6)
        records.append(
            {
                "instance_id": instance,
                "run_summary": {
                    "status": "completed",
                    "reason": None,
                    "tokens": 260_000,
                    "steps": 16,
                    "duration_s": 900.0,
                },
                "workflow_result": {
                    "status": "done",
                    "seat_cap": 2_000_000,
                    "edges_declared": declared,
                    "edges_walked": walked,
                },
            }
        )
    _write_metrics(cell, records)
    return cell


# --- E13: the censoring indicator only one arm wrote ------------------------ #
#
# ``wall_clock_timeout`` is written by ``gen_prediction_agent.py:154`` and by
# nothing else, so only the single arm carries a censoring indicator -- and the
# wall-clock window is *shorter* on the other two, which take the remainder
# after container start (arm_registry.py:476-516). The two arms most likely to
# be censored were the two with no indicator.
#
# The rule does not have to be invented. ``gen_prediction_agent.py:102`` builds
# that flag as ``status == "stopped" and reason == "timeout"``, and
# ``gen_prediction_workflow.py:184-203`` writes the very same pair into
# ``run_summary`` on the workflow and team arms from ``runtime_status`` /
# ``runtime_reason`` (``runtime_reason == "timeout"`` is a value that path
# knows, :300). So one rule reads all three arms off the block they share.


def _timeout_cell(tmp_path: Path) -> Path:
    """One team run cut off at the wall and two that were not."""
    cell = tmp_path / "team-timeout"
    for instance in (
        "matplotlib__matplotlib-24870",
        "django__django-13933",
        # Stopped, but by its budget: a stop is not a censoring.
        "sympy__sympy-15875",
    ):
        runtime = _runtime_dir(cell, "team", instance)
        _seat_file(runtime, "agent_0_analyst-aa.json", aid=0, role="analyst", tokens=500_000, assistant=20)
    _write_metrics(
        cell,
        [
            {
                "instance_id": "matplotlib__matplotlib-24870",
                "run_summary": {
                    "status": "stopped",
                    "reason": "timeout",
                    "tokens": 3_603_791,
                    "steps": 49,
                    "duration_s": 5401.108277289197,
                },
            },
            {
                "instance_id": "django__django-13933",
                "run_summary": {
                    "status": "completed",
                    "reason": None,
                    "tokens": 552_798,
                    "steps": 48,
                    "duration_s": 1902.95,
                },
            },
            {
                "instance_id": "sympy__sympy-15875",
                "run_summary": {
                    "status": "stopped",
                    "reason": PRECHECK_TERMINAL,
                    "tokens": 1_951_372,
                    "steps": 56,
                    "duration_s": 3200.0,
                },
            },
        ],
    )
    return cell


# --- E13: the team arm's declared edges, which no report read --------------- #
#
# ``edges_declared`` was read only out of ``workflow_result``, which only the
# scripted workflow writes, so both team batches of 2026-09-04 reported
# ``edges_declared = 0`` and ``edges_walked_rate = None``. The team arm does
# declare its topology: ``_scheduler_team.py:441-460`` writes one
# ``assigned.topology_edges`` event per run into ``trajectory.jsonl``, and the
# real runs carry six edges over three roles. "Nobody may talk" and "this
# report never looked" were spelt the same way.
#
# Alpha and edges are not the same quantity and this does not merge them.
# Alpha is the rate at which an agent *chose* to hand work on -- one number per
# run, over runs. An edge is a channel the team file declared; walking it is
# one agent addressing another over that channel at least once. A run can walk
# an edge without delivering (a message that carried nothing on), and alpha
# stays the ladder's number.


def _topology_log(directory: Path, edges: list[tuple[str, str]], allow_all: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "assigned.topology_nodes", "payload": {"declared_roles": ["analyst", "coder", "tester"]}},
        {
            "type": "assigned.topology_edges",
            "payload": {
                "allow_all": allow_all,
                "declared_roles": ["analyst", "coder", "tester"],
                "edges": [{"from_role": f, "to_role": t} for f, t in edges],
            },
        },
    ]
    with (directory / "trajectory.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in rows))


def _seat_with_messages(directory: Path, filename: str, *, aid: int, role: str, targets: list[dict]) -> None:
    """A team seat that made one ``message_agent`` call per entry in ``targets``."""
    directory.mkdir(parents=True, exist_ok=True)
    messages = [
        {
            "role": "assistant",
            "content": "handing over",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "message_agent", "arguments": json.dumps(t)}}],
        }
        for i, t in enumerate(targets)
    ]
    role_aids = {"analyst": 0, "adopter": 0, "coder": 1, "coder_a": 1, "tester": 2, "coder_b": 2}
    for i, target in enumerate(targets):
        destination = target.get("to_aid", role_aids.get(target.get("to_role")))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "content": f"Message queued to aid {destination}." if destination is not None else "Error: no target.",
            }
        )
    (directory / filename).write_text(
        json.dumps(
            {
                "aid": aid,
                "role": role,
                "session_state": {"used_tokens": 10_000, "step_count": len(targets), "terminal_reason": ""},
                "messages": messages,
            }
        ),
        encoding="utf-8",
    )


SIX_EDGES = [
    ("analyst", "coder"),
    ("analyst", "tester"),
    ("coder", "analyst"),
    ("coder", "tester"),
    ("tester", "analyst"),
    ("tester", "coder"),
]


@pytest.fixture
def team_edges_cell(tmp_path: Path) -> Path:
    """Three team runs on the six-edge topology the real batches declare."""
    cell = tmp_path / "team-edges"
    records = []
    for instance, analyst_targets in (
        ("a", [{"to_role": "coder", "summary": "s", "content": "c"}]),
        (
            "b",
            [
                {"to_aid": 2, "summary": "s", "content": "c"},
                {"to_role": "coder", "summary": "s", "content": "c"},
                # A pair this team file never declared. The scheduler refuses
                # it, so it is not a channel that carried anything.
                {"to_role": "reviewer", "summary": "s", "content": "c"},
            ],
        ),
        ("c", []),
    ):
        runtime = _runtime_dir(cell, "team", instance)
        _seat_with_messages(runtime, "agent_0_analyst-aa.json", aid=0, role="analyst", targets=analyst_targets)
        _seat_file(runtime, "agent_1_coder-bb.json", aid=1, role="coder", tokens=5_000, assistant=2)
        _seat_file(runtime, "agent_2_tester-cc.json", aid=2, role="tester", tokens=5_000, assistant=2)
        _topology_log(runtime, SIX_EDGES)
        records.append(
            {
                "instance_id": instance,
                "run_summary": {"status": "completed", "reason": None, "tokens": 20_000, "steps": 4},
            }
        )
    _write_metrics(cell, records)
    return cell


def _two_candidate_cell(tmp_path: Path, *, coder_b_worked: bool) -> Path:
    """The two-candidate roster: an Adopter and two Coders that cannot see each other."""
    cell = tmp_path / "s2dual"
    runtime = _runtime_dir(cell, "team", "a")
    _seat_with_messages(
        runtime,
        "agent_0_adopter-aa.json",
        aid=0,
        role="adopter",
        targets=[{"to_role": "coder_a", "summary": "s", "content": "c"}],
    )
    _seat_file(runtime, "agent_1_coder_a-bb.json", aid=1, role="coder_a", tokens=5_000, assistant=2)
    _seat_file(
        runtime,
        "agent_2_coder_b-cc.json",
        aid=2,
        role="coder_b",
        tokens=5_000 if coder_b_worked else 0,
        assistant=2 if coder_b_worked else 0,
    )
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
    _write_metrics(
        cell,
        [{"instance_id": "a", "run_summary": {"status": "completed", "tokens": 20_000, "steps": 4}}],
    )
    return cell
