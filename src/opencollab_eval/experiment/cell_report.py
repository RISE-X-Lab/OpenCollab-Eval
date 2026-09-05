"""What one cell's runs did, read from the files the driver wrote.

Per run: status and tokens from ``metrics.jsonl``; per seat, tokens, steps,
assistant turns, ``message_agent`` calls and write-tool calls from the agent
files the runtime autosaves -- under ``logs-<arm>/<instance>/trajectories`` for
the team and workflow arms, and at ``<cell>/agent-<hex>/agent.json`` for the
single arm, where only the run's own ``trajectory_path`` says which directory
belongs to which instance.
"Delivered" is the delegation criterion the ladder is read on: a coder or
tester seat that spent tokens *and* produced at least one assistant turn.

The denominator is stated, not assumed: runs that failed before any model step
are listed and excluded; a run stopped at its seat cap is valid. The interval
is Clopper-Pearson, so 0 of n and n of n get honest bounds. So is the reading
itself: a run whose seat snapshot was never located reads as zero seats, no
delivery and no cap -- the same as a run that spent nothing and stopped at
nothing -- so every row carries ``seat_snapshot_found`` and the report names
the runs where the counts below are floors rather than facts.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from math import comb
from pathlib import Path
from typing import Any

WRITE_TOOLS = frozenset({"apply_patch", "file_write"})
DELEGATE_ROLES = frozenset({"coder", "tester"})
#: Arms whose delivery rate is alpha: the rate at which an agent *chose* to
#: hand work on. Deliberately narrower than ``DELIVERY_READABLE_ARMS``, which
#: is the wider and different question "does this arm seat more than one role
#: worth counting" -- a scripted workflow does, and its seats are read as they
#: were. What it does not have is a choice: ``self_collaboration.py`` sequences
#: its edges, so a delivery rate there measures the script, and reporting one
#: put a Clopper-Pearson interval on the wrong quantity (0.750 (0.194, 0.994)
#: on the smoke DW batch). What varies on that arm is whether a scripted edge
#: carried anything, which the arm records per run as ``edges_walked``.
ALPHA_READABLE_ARMS = frozenset({"team"})
# A run the model never touched: no denominator for anything.
INVALID_STATUSES = frozenset({"failed", "error"})
# The runtime stamps every seat of a scripted workflow with one generic role,
# so a workflow seat's identity lives in its file name instead. A team seat
# carries its own role and is read from the record as before.
GENERIC_WORKFLOW_ROLE = "workflow_agent"
# Two different terminal events, both spelt with the word "budget". The first
# is the pre-call input reservation refusing to enter a call the seat could
# still have afforded; the second is the seat actually overspending after a
# call returned. Only the second one means "this run's outcome was chosen by
# the cap", so the ladder's capped column cannot be read off their union.
CAP_PRECHECK_MARKER = "before model call: conservative input reservation"
CAP_POSTCALL_MARKER = "budget exceeded after model call"
# The runtime writes four budget stops, not two. These are the other two:
# ``session_run.py:616`` fires the precheck on spend already made, and
# ``session_run.py:625`` is the aggregate ceiling a whole team draws against.
# Both matched the bare ``"budget" in terminal`` test that fills ``cap_hit``
# and neither of the two columns that split it, so a run stopped by either was
# counted once and attributed nowhere. Matched on the prefix, because the
# aggregate string contains the per-session one.
CAP_PRECHECK_SPENT_PREFIX = "budget exceeded: "
CAP_AGGREGATE_PREFIX = "team budget exceeded"
# NOT a cap, and the reason this had to be established rather than assumed: on
# a scripted workflow every healthy seat ends with this string. It is the
# structured-output capture -- ``workflow_structured.py:137-138`` sets a cancel
# event when ``structured_output`` is accepted and ``session_run.py:604-606``
# turns that into this reason at the next precheck. A seat that stopped here
# stopped because it had answered, whatever it had left.
STRUCTURED_CAPTURE_TERMINAL = "interrupted by user"

#: The run's ordered event log, written beside its seat snapshots:
#: ``trajectory.jsonl`` on the single and team arms, ``orchestration.jsonl`` on
#: a scripted workflow. It is the only place the allowance a seat was drawing
#: against is recorded -- ``session_state`` carries ``used_tokens`` and no
#: ceiling at all -- so without it a seat that finished with 0.25% of its
#: allowance left and one that finished with 90% left are the same row.
EVENT_LOG_NAMES = ("trajectory.jsonl", "orchestration.jsonl")
#: The row inside that log which names a session's disposition and both of its
#: ceilings (``session_run.py:333-380``).
SESSION_TERMINAL_EVENT = "session_terminal"
#: The row that names the topology the run was *assigned*, written once at
#: prebuild by ``_scheduler_team.py:441-460``: ``allow_all``, ``declared_roles``
#: and ``edges`` as ``{from_role, to_role}`` pairs, verbatim from the team file.
#: It is the team arm's answer to the question the scripted workflow answers in
#: ``workflow_result.edges_declared``, and until it was read the team cells
#: reported no declared edges at all -- which is how an arm that declares none
#: reads.
ASSIGNED_TOPOLOGY_EVENT = "assigned.topology_edges"
#: The tool one seat addresses another with. A declared edge is *walked* when a
#: seat holding its ``from_role`` made at least one ``message_agent`` call that
#: resolved to its ``to_role``. That is not alpha: alpha is whether an agent
#: chose to hand the work on (a coder or tester seat that spent tokens and
#: spoke), one number per run; walking an edge is whether a declared channel
#: carried anything at all. A run can walk an edge and deliver nothing.
MESSAGE_TOOL = "message_agent"


def binom_cdf(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(0, k + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval by bisection on the binomial CDF."""
    if n == 0:
        return (float("nan"), float("nan"))
    if k == 0:
        low = 0.0
    else:
        a, b = 0.0, 1.0
        for _ in range(200):
            m = (a + b) / 2
            if 1 - binom_cdf(k - 1, n, m) > alpha / 2:
                b = m
            else:
                a = m
        low = (a + b) / 2
    if k == n:
        high = 1.0
    else:
        a, b = 0.0, 1.0
        for _ in range(200):
            m = (a + b) / 2
            if binom_cdf(k, n, m) < alpha / 2:
                b = m
            else:
                a = m
        high = (a + b) / 2
    return low, high


@dataclass
class Seat:
    role: str
    tokens: int = 0
    steps: int = 0
    assistant: int = 0
    writes: int = 0
    msg_agent: int = 0
    terminal: str = ""
    #: Who this seat addressed, one entry per ``message_agent`` call, as the
    #: call itself named the target: ``role:<name>`` or ``aid:<n>`` (the tool
    #: takes exactly one of ``to_role`` and ``to_aid``). Kept as written so the
    #: aid can be resolved against this run's own roster rather than guessed.
    msg_agent_targets: list[str] = field(default_factory=list)
    #: The allowance this session was handed, from its ``session_terminal``

    #: event. ``None`` means the event log was not there to read, which is not
    #: the same as an allowance of zero and must never be printed as one.
    cap: int | None = None


@dataclass
class RunRow:
    instance_id: str
    status: str
    reason: str
    tokens: int
    steps: int
    seats: dict[str, Seat] = field(default_factory=dict)
    #: What each *role* spent, summed over every seat that held it. A scripted
    #: workflow seats its analyst twice -- once to write the brief and once to
    #: adjudicate -- so a per-role lookup built by role alone keeps whichever
    #: seat it read last and silently discards the other. Reading the analyst
    #: column off ``seats`` that way printed 48,695 on a run whose analyst
    #: spent 1,995,025.
    role_tokens: dict[str, int] = field(default_factory=dict)
    #: How many seats held each role, so a summed column says how many numbers
    #: it is the sum of.
    role_seats: dict[str, int] = field(default_factory=dict)
    #: The workflow's own per-seat ledger (``workflow_result.seat_spend``),
    #: kept beside the sum above rather than in place of it: two independent
    #: readings of the same quantity, so a disagreement is visible instead of
    #: being settled by whichever one the report happened to print.
    seat_spend_recorded: dict[str, int] | None = None
    seat_spend_agrees: bool | None = None
    delivered: bool = False
    tree_snapshots: int = 0
    cap_hit: list[str] = field(default_factory=list)
    cap_hit_precheck: list[str] = field(default_factory=list)
    cap_hit_postcall: list[str] = field(default_factory=list)
    #: The aggregate ceiling, kept apart from the per-seat stops for the same
    #: reason those two are kept apart: it is a different stop.
    cap_hit_aggregate: list[str] = field(default_factory=list)
    #: The allowance one seat of this run was given, recorded: the workflow's
    #: own ``seat_cap`` where the arm sets one, else the largest
    #: ``max_budget_tokens`` in the run's ``session_terminal`` events.
    seat_cap: int | None = None
    #: Derived from the two above: allowance minus the role's summed spend.
    role_headroom: dict[str, int] = field(default_factory=dict)
    seat_headroom_min: int | None = None
    #: Recorded, and by the arm that owns the rule: the scripted workflow
    #: writes ``status="budget_exhausted"`` when its own ``exhausted(seat)``
    #: check stops a round.
    budget_exhausted: bool = False
    #: Declared topology, and how much of it carried anything. Recorded by the
    #: arm that scripts the topology (``workflow_result.edges_walked`` against
    #: ``edges_declared``); ``None`` on the arms that declare no edges, which
    #: is not the same as an arm that declared some and walked none.
    edges_walked: int | None = None
    edges_declared: int | None = None
    #: How long the run took, from the block every arm writes.
    duration_s: float | None = None
    #: Whether the wall clock ended this run, by the rule the single arm's own
    #: ``wall_clock_timeout`` is built from -- so the two arms that never wrote
    #: that flag, and whose wall-clock window is the *shorter* one, get the
    #: same indicator from the same evidence.
    timeout_censored: bool = False
    #: The single arm's own flag where the record carries it, kept beside the
    #: derived reading rather than replacing it, so the two can be compared.
    timeout_recorded: bool | None = None
    patch_chars: int | None = None
    card: str | None = None
    team_config: str | None = None
    #: Whether this run's seat snapshot was located at all. Every quantity read
    #: off a seat -- ``seats``, ``delivered`` and all three cap columns -- is
    #: zero or empty both when the run's seats did nothing and when the files
    #: holding what they did were never opened, and the two readings are
    #: identical in the report. This says which one it is.
    seat_snapshot_found: bool = False
    #: Which attempt at this instance this row is, and how many attempts the
    #: cell holds. A run the endpoint dropped leaves a prediction row behind,
    #: so the original batch cannot be resumed into; the second attempt runs in
    #: its own out-dir and is merged back here. Both numbers are on the row
    #: because "this instance ran once" and "this instance ran twice and the
    #: first attempt is not the one reported" are different facts.
    attempt: int = 1
    attempts: int = 1
    #: The out-dir this row was read from. Equal to the cell's own name unless
    #: the row came from a retry batch.
    source_batch: str = ""

    @property
    def valid(self) -> bool:
        return self.status not in INVALID_STATUSES


#: How each arm names the per-seat snapshot the runtime autosaves, relative to
#: one instance's ``trajectories`` root. A team writes
#: ``agent_<aid>_<role>-<hex>.json``; a scripted workflow writes
#: ``<nnn>_<label>.json`` (``000_analyst.json``, ``001_coder-r1.json``), which
#: the team pattern does not match -- so a DW cell used to read as zero seats,
#: zero delivery and no cap, with no error anywhere saying the files were never
#: opened. The journal sidecars (``*.json.journal``) do not match either
#: pattern, which is why neither has to exclude them.
_SEAT_FILE_PATTERNS = ("*/*/agent_*.json", "*/*/[0-9][0-9][0-9]_*.json")


#: Arms whose seat snapshot is not under ``logs-<arm>/<instance>/``. The
#: single-agent arm autosaves one directory per run at the batch root,
#: ``<cell>/agent-<hex>/agent.json``, named by a random id that carries no
#: instance -- so no glob under ``logs-single/<instance>/`` finds it, and every
#: single run read as zero seats, which is also how a run that never hit its
#: cap reads. That is why ``cap_hit``, ``cap_hit_precheck`` and
#: ``cap_hit_postcall`` were empty for the whole arm while the same runs
#: carried the pre-call reservation verbatim in ``run_summary.reason``.
SEAT_AT_BATCH_ROOT_ARMS = frozenset({"single"})
#: What that arm calls its snapshot inside the directory the record names.
SINGLE_SEAT_FILE = "agent.json"


def _agent_files(cell: Path, arm: str, instance_id: str) -> list[Path]:
    """Per-seat snapshots for one run, for the arms that write them per instance."""
    root = cell / f"logs-{arm}" / instance_id / "trajectories"
    if not root.exists():
        return []
    found: set[Path] = set()
    for pattern in _SEAT_FILE_PATTERNS:
        found.update(root.glob(pattern))
    return sorted(found)


def _single_agent_files(cell: Path, record: dict[str, Any]) -> list[Path]:
    """The one seat of a single-agent run, found through the run's own record.

    Nothing in the directory name ties ``agent-<hex>`` to an instance, so the
    tie has to come from ``metrics.jsonl``, which records the directory as
    ``trajectory_path``. That path was written on the machine that ran the
    batch, so it is resolved by name under the pulled cell first and taken
    verbatim only when the batch is read where it ran. A record without the
    field, or whose directory is not here, yields no seat -- the same reading
    as before, never a wrong one.
    """
    raw = str(record.get("trajectory_path") or "")
    if not raw:
        return []
    named = Path(raw)
    for directory in (cell / named.name, named):
        snapshot = directory / SINGLE_SEAT_FILE
        if snapshot.is_file():
            return [snapshot]
    return []


def _seat_files(cell: Path, arm: str, record: dict[str, Any]) -> list[Path]:
    """The snapshots of one run, from whichever place its arm keeps them."""
    if arm in SEAT_AT_BATCH_ROOT_ARMS:
        return _single_agent_files(cell, record)
    return _agent_files(cell, arm, record["instance_id"])


def _int_or_none(value: Any) -> int | None:
    """An integer when the record holds one, else ``None`` -- never ``0``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _event_log_facts(seat_paths: list[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """The two things this run's event log records: its sessions and its topology.

    Returns the ``session_terminal`` payload of each session keyed by ``aid``,
    and the single ``assigned.topology_edges`` payload, or ``None`` when the
    run wrote none. Read from the event log beside the seat snapshots, and read
    defensively: these are the only quantities in the report that come from a
    second file, and a batch pulled before the log existed, a truncated log, or
    a log this reader cannot parse must all leave every other column exactly as
    it was.

    The logs run to hundreds of megabytes a batch and hold a handful of these
    rows, so a line that can contain neither is skipped before it is parsed.
    """
    found: dict[str, dict[str, Any]] = {}
    topology: dict[str, Any] | None = None
    for directory in dict.fromkeys(path.parent for path in seat_paths):
        for name in EVENT_LOG_NAMES:
            log = directory / name
            if not log.is_file():
                continue
            try:
                with log.open(encoding="utf-8") as handle:
                    for line in handle:
                        if SESSION_TERMINAL_EVENT not in line and ASSIGNED_TOPOLOGY_EVENT not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        kind = event.get("type")
                        payload = event.get("payload") or {}
                        if kind == SESSION_TERMINAL_EVENT:
                            found[str(payload.get("aid"))] = payload
                        elif kind == ASSIGNED_TOPOLOGY_EVENT:
                            topology = payload
            except OSError:
                continue
    return found, topology


def declared_edges(topology: dict[str, Any] | None) -> set[tuple[str, str]] | None:
    """The edge set a run was assigned, or ``None`` when it declared none.

    ``allow_all`` travels with the edges precisely because an open topology
    declares no edges at all (``_scheduler_team.py:425-427``): its ``edges``
    list is empty, and an empty list read as a declaration would say "nobody
    may talk", which is its opposite. So an open topology is ``None`` here, the
    same answer as a run that wrote no topology event -- while a closed
    topology whose list *is* empty returns the empty set, because "this team
    file lets nobody address anybody" is a declaration and has to be counted
    as one.
    """
    if not topology or topology.get("allow_all"):
        return None
    edges = topology.get("edges")
    if not isinstance(edges, list):
        return None
    return {
        (str(edge.get("from_role")), str(edge.get("to_role")))
        for edge in edges
        if isinstance(edge, dict) and edge.get("from_role") and edge.get("to_role")
    }


def walked_edges(seats: dict[str, Seat], declared: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which declared edges carried at least one message.

    A call addressed ``to_aid`` is resolved against this run's own roster, so
    the same edge addressed by role and by id counts once. A call to a pair the
    team file never declared is not counted: the scheduler refuses it
    (``_topology_forbids``), and counting it would put a refusal in the column
    that says a channel carried something.
    """
    role_of_aid = {aid: seat.role for aid, seat in seats.items()}
    walked: set[tuple[str, str]] = set()
    for seat in seats.values():
        for target in seat.msg_agent_targets:
            kind, _, value = target.partition(":")
            to_role = value if kind == "role" else role_of_aid.get(value)
            if to_role and (seat.role, to_role) in declared:
                walked.add((seat.role, to_role))
    return walked


def _seat_role(path: Path, recorded: str) -> str:
    """The seat's role, from the record when it names one, else from the file.

    A team seat records ``analyst`` / ``coder`` / ``tester`` and is taken as
    written. A workflow seat records the generic ``workflow_agent`` for every
    seat, so ``delivered`` -- "a coder or tester seat spent tokens and spoke"
    -- was false for every DW run no matter what the run did. The name the
    driver gave the file is where that arm's seat identity actually is:
    ``001_coder-r1`` is the coder, ``003_analyst-adjudicate-r1`` is the analyst
    coming back to adjudicate.
    """
    if recorded and recorded != GENERIC_WORKFLOW_ROLE:
        return recorded
    label = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
    return label.split("-", 1)[0] or recorded


def _read_seat(path: Path) -> tuple[int, Seat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    state = data.get("session_state") or {}
    messages = data.get("messages") or []
    calls = []
    targets: list[str] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name") or ""
            calls.append(name)
            if name != MESSAGE_TOOL:
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            if arguments.get("to_role"):
                targets.append(f"role:{arguments['to_role']}")
            elif arguments.get("to_aid") is not None:
                targets.append(f"aid:{arguments['to_aid']}")
            else:
                # The tool requires exactly one of the two; a call with
                # neither was refused and addressed nobody.
                targets.append("unaddressed")
    seat = Seat(
        role=_seat_role(path, str(data.get("role") or "")),
        tokens=int(state.get("used_tokens") or 0),
        steps=int(state.get("step_count") or 0),
        assistant=sum(1 for m in messages if m.get("role") == "assistant"),
        writes=sum(1 for name in calls if name in WRITE_TOOLS),
        msg_agent=sum(1 for name in calls if name == MESSAGE_TOOL),
        terminal=str(state.get("terminal_reason") or ""),
        msg_agent_targets=targets,
    )
    return int(data.get("aid") or 0), seat


def run_rows(cell: str | Path, arm: str = "team") -> list[RunRow]:
    cell = Path(cell)
    metrics = cell / "metrics.jsonl"
    if not metrics.exists():
        raise FileNotFoundError(f"{metrics} not found; pull the batch first")
    rows: list[RunRow] = []
    with metrics.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            summary = record.get("run_summary") or {}
            # A workflow arm records its seat boundaries, its seat ledger and
            # its own stop verdict inside its own result rather than at the top
            # level (see ``_result_metrics``), so a DW run read only at the top
            # level shows zero snapshots -- the same reading an arm that records
            # none produces.
            workflow_result = record.get("workflow_result")
            seat_paths = _seat_files(cell, arm, record)
            terminals, topology = _event_log_facts(seat_paths)
            seats: dict[str, Seat] = {}
            for path in seat_paths:
                aid, seat = _read_seat(path)
                seat.cap = _int_or_none((terminals.get(str(aid)) or {}).get("max_budget_tokens"))
                seats[str(aid)] = seat
            snapshots = record.get("tree_snapshots")
            if not snapshots and isinstance(workflow_result, dict):
                snapshots = workflow_result.get("tree_snapshots")
            role_tokens: dict[str, int] = {}
            role_seats: dict[str, int] = {}
            for seat in seats.values():
                role_tokens[seat.role] = role_tokens.get(seat.role, 0) + seat.tokens
                role_seats[seat.role] = role_seats.get(seat.role, 0) + 1
            recorded_spend = None
            seat_cap = None
            budget_exhausted = False
            edges_walked: int | None = None
            edges_declared: int | None = None
            if isinstance(workflow_result, dict):
                raw_spend = workflow_result.get("seat_spend")
                if isinstance(raw_spend, dict):
                    recorded_spend = {str(k): int(v or 0) for k, v in raw_spend.items()}
                # The scripted workflow sets its own seat allowance and records
                # it; ``exhausted()`` (self_collaboration.py:466-467) is what
                # writes the verdict below when a round stops for want of one.
                seat_cap = _int_or_none(workflow_result.get("seat_cap"))
                budget_exhausted = workflow_result.get("status") == "budget_exhausted"
                declared = workflow_result.get("edges_declared")
                if isinstance(declared, list):
                    edges_declared = len(declared)
                    walked = workflow_result.get("edges_walked")
                    edges_walked = len(walked) if isinstance(walked, list) else 0
            if edges_declared is None:
                # The team arm declares its topology in the run's own event
                # log rather than in a workflow result. Read the same way and
                # reported in the same two columns, so the two arms' edge
                # counts mean the same thing.
                assigned = declared_edges(topology)
                if assigned is not None:
                    edges_declared = len(assigned)
                    edges_walked = len(walked_edges(seats, assigned))
            if seat_cap is None:
                # Otherwise the allowance is the largest ceiling any of this
                # run's sessions was handed: a session that resumes a partly
                # spent seat is given what the seat had left, not the seat.
                caps = [s.cap for s in seats.values() if s.cap]
                seat_cap = max(caps) if caps else None
            # Derived, not recorded: nothing writes down what a seat had left
            # when it stopped, so this is the recorded allowance minus the
            # role's summed spend. Empty when the allowance is unknown --
            # printing zero there would make every unread run look fully spent.
            role_headroom = (
                {role: seat_cap - spent for role, spent in role_tokens.items()}
                if seat_cap is not None
                else {}
            )
            row = RunRow(
                instance_id=record["instance_id"],
                status=str(summary.get("status") or record.get("runtime_status") or ""),
                reason=str(summary.get("reason") or ""),
                tokens=int(summary.get("tokens") or record.get("tokens_used") or 0),
                steps=int(summary.get("steps") or 0),
                seats=seats,
                role_tokens=role_tokens,
                role_seats=role_seats,
                seat_spend_recorded=recorded_spend,
                seat_spend_agrees=(
                    None
                    if recorded_spend is None
                    else {k: v for k, v in role_tokens.items() if v or k in recorded_spend}
                    == {k: v for k, v in recorded_spend.items() if v or k in role_tokens}
                ),
                delivered=any(s.role in DELEGATE_ROLES and s.tokens > 0 and s.assistant > 0 for s in seats.values()),
                tree_snapshots=len(snapshots or []),
                cap_hit=[aid for aid, s in seats.items() if "budget" in s.terminal.lower()],
                cap_hit_precheck=[
                    aid
                    for aid, s in seats.items()
                    if CAP_PRECHECK_MARKER in s.terminal
                    or s.terminal.startswith(CAP_PRECHECK_SPENT_PREFIX)
                ],
                cap_hit_postcall=[
                    aid for aid, s in seats.items() if CAP_POSTCALL_MARKER in s.terminal
                ],
                cap_hit_aggregate=[
                    aid for aid, s in seats.items() if s.terminal.startswith(CAP_AGGREGATE_PREFIX)
                ],
                seat_cap=seat_cap,
                role_headroom=role_headroom,
                seat_headroom_min=min(role_headroom.values()) if role_headroom else None,
                budget_exhausted=budget_exhausted,
                edges_walked=edges_walked,
                edges_declared=edges_declared,
                duration_s=(
                    float(summary["duration_s"])
                    if isinstance(summary.get("duration_s"), int | float)
                    else None
                ),
                # ``gen_prediction_agent.py:102`` verbatim. The workflow and
                # team paths write the same pair into ``run_summary`` from
                # ``runtime_status``/``runtime_reason``
                # (gen_prediction_workflow.py:184-203), so this one rule reads
                # all three arms off the block they share.
                timeout_censored=(
                    str(summary.get("status") or "") == "stopped"
                    and str(summary.get("reason") or "") == "timeout"
                ),
                timeout_recorded=(
                    bool(record["wall_clock_timeout"])
                    if "wall_clock_timeout" in record
                    else None
                ),
                patch_chars=record.get("submitted_patch_chars"),
                card=(record.get("role_prompt_sha256") or {}).get("analyst"),
                team_config=record.get("team_config_path"),
                seat_snapshot_found=bool(seats),
            )
            rows.append(row)
    return rows


def merge_attempts(batches: list[tuple[str, list[RunRow]]]) -> list[RunRow]:
    """One row per instance, from the attempts made at it, in attempt order.

    ``batches`` is ``[(out-dir name, its rows), ...]``, oldest first. Six runs
    on 2026-09-04 ended ``failed`` with ``APIError: Upstream request failed``:
    the endpoint dropped them, not the model. Each still wrote a prediction
    row, so re-launching the original batch resumes nothing; the second attempt
    runs in an out-dir of its own and is folded back in here.

    The rule, and it is the only one that does not quietly change a
    denominator: an instance reports its **last attempt that ran** -- the last
    whose status is not in ``INVALID_STATUSES``. When no attempt ran, the last
    one is kept rather than dropped, because an instance that vanishes from the
    table is an instance nobody counts as lost. Earlier attempts are not summed
    into anything: what they spent was spent, but the cell is one observation
    per instance and a token total over two attempts at one instance is a
    number about the endpoint, not about the arm.
    """
    order: list[str] = []
    tries: dict[str, list[tuple[str, RunRow]]] = {}
    for name, rows in batches:
        for row in rows:
            if row.instance_id not in tries:
                tries[row.instance_id] = []
                order.append(row.instance_id)
            tries[row.instance_id].append((name, row))
    merged: list[RunRow] = []
    for instance_id in order:
        made = tries[instance_id]
        chosen = len(made) - 1
        for index in range(len(made) - 1, -1, -1):
            if made[index][1].valid:
                chosen = index
                break
        name, row = made[chosen]
        row.attempt = chosen + 1
        row.attempts = len(made)
        row.source_batch = name
        merged.append(row)
    return merged


def order_rows(rows: list[RunRow], order_csv: str | Path | None) -> tuple[list[RunRow], list[str]]:
    """Rows in the suite's frozen order, plus the instances the suite has but the cell does not."""
    if order_csv is None:
        return rows, []
    with Path(order_csv).open(encoding="utf-8", newline="") as handle:
        wanted = [r["instance_id"] for r in csv.DictReader(handle)]
    index = {r.instance_id: r for r in rows}
    ordered = [index[i] for i in wanted if i in index]
    extra = [r for r in rows if r.instance_id not in set(wanted)]
    return ordered + extra, [i for i in wanted if i not in index]


def summarize(
    rows: list[RunRow],
    expected_card: str | None = None,
    team: bool = True,
    alpha_readable: bool | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Counts over the cell.

    ``team`` says whether the cell has seats to lay out; ``alpha_readable``
    says whether its delivery rate is alpha. They are different questions and
    used to be one flag. A single agent has nobody to deliver to, so alpha is
    undefined there, not zero -- and a scripted workflow *has* the seats but
    makes no choice, so its delivery rate measures its script. It defaults to
    ``team`` so every existing caller reads exactly as before.
    """
    valid = [r for r in rows if r.valid]
    if alpha_readable is None:
        alpha_readable = team
    delivered = sum(1 for r in valid if r.delivered) if alpha_readable else None
    low, high = clopper_pearson(delivered, len(valid)) if alpha_readable else (None, None)
    cards = sorted({r.card for r in rows if r.card})
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    return {
        "runs": len(rows),
        "valid": len(valid),
        "invalid": [r.instance_id for r in rows if not r.valid],
        "team": team,
        "alpha_readable": alpha_readable,
        "delivered": delivered,
        "alpha": (delivered / len(valid)) if (alpha_readable and valid) else None,
        "ci95": [low, high] if alpha_readable else None,
        # What the arm that scripts its topology reports instead: not a rate at
        # which an agent chose, a count of how much of a fixed topology carried
        # anything.
        "edges_walked": sum(r.edges_walked or 0 for r in rows if r.edges_declared),
        "edges_declared": sum(r.edges_declared or 0 for r in rows),
        "edges_walked_rate": (
            sum(r.edges_walked or 0 for r in rows if r.edges_declared)
            / sum(r.edges_declared or 0 for r in rows)
            if sum(r.edges_declared or 0 for r in rows)
            else None
        ),
        # Averaging hides the shape: one run walking none of its six and six
        # runs each missing one are the same rate.
        # Whether an edge set was declared at all. ``edges_declared == 0`` is
        # what an arm that declares none and an arm whose declaration was never
        # read both produce, and those are different findings.
        "edges_declared_state": (
            "declared" if any(r.edges_declared is not None for r in rows) else "not_declared"
        ),
        "edges_unwalked": [
            [r.instance_id, r.edges_walked or 0, r.edges_declared]
            for r in rows
            if r.edges_declared and (r.edges_walked or 0) < r.edges_declared
        ],
        "statuses": statuses,
        # The retry ledger. Present on every cell, including the ones nothing
        # was retried in, so "no instance needed a second attempt" and "this
        # report does not say" are not the same blank.
        "retried": [r.instance_id for r in rows if r.attempts > 1],
        "retried_count": sum(1 for r in rows if r.attempts > 1),
        # A second attempt that ran where the first did not.
        "retry_succeeded": [r.instance_id for r in rows if r.attempts > 1 and r.attempt > 1 and r.valid],
        "retry_succeeded_count": sum(1 for r in rows if r.attempts > 1 and r.attempt > 1 and r.valid),
        # No attempt at this instance ever reached the model. Identical to
        # ``invalid`` on a cell with no retries, and deliberately so: it is the
        # same fact, counted after every attempt has been made.
        "infra_failed": [r.instance_id for r in rows if not r.valid],
        "infra_failed_count": sum(1 for r in rows if not r.valid),
        "attempt_sources": sorted({r.source_batch for r in rows if r.source_batch}),
        "cap_hit": [r.instance_id for r in rows if r.cap_hit],
        # The two halves of ``cap_hit``, kept apart because only the second is
        # a run whose outcome the budget chose.
        "cap_hit_precheck": [r.instance_id for r in rows if r.cap_hit_precheck],
        "cap_hit_postcall": [r.instance_id for r in rows if r.cap_hit_postcall],
        "cap_hit_aggregate": [r.instance_id for r in rows if r.cap_hit_aggregate],
        # Recorded: the allowances this cell's sessions were actually handed.
        # More than one value is not a fault -- a session resuming a partly
        # spent seat is handed the remainder -- but a cell whose largest value
        # is not the budget the spec declared is a cell that did not run at the
        # budget it says it ran at.
        "seat_cap_tokens": sorted({r.seat_cap for r in rows if r.seat_cap is not None}),
        "seat_cap_unknown": [r.instance_id for r in rows if r.seat_cap is None],
        # The workflow's own verdict, not this reader's.
        "seat_budget_exhausted": [r.instance_id for r in rows if r.budget_exhausted],
        # Derived. The tightest runs first, so a run that finished on a seat it
        # had all but spent is visible without re-deriving the subtraction.
        "seat_headroom_min": sorted(
            (
                [r.instance_id, r.seat_headroom_min]
                for r in rows
                if r.seat_headroom_min is not None
            ),
            key=lambda pair: pair[1],
        ),
        # Not a quantity about the runs: a quantity about the reading of them.
        # Every count below that comes off a seat has this as its denominator,
        # so a cell where it is short of ``runs`` has counts that are floors.
        "seat_snapshot_found": sum(1 for r in rows if r.seat_snapshot_found),
        "seat_snapshot_missing": [r.instance_id for r in rows if not r.seat_snapshot_found],
        "seat_snapshot_missing_count": sum(1 for r in rows if not r.seat_snapshot_found),
        # The runs where summing the seat files and the workflow's own
        # ``seat_spend`` ledger do not agree. Empty is the expected reading;
        # a non-empty list means one of the two is wrong and neither column
        # can be quoted until it is settled.
        "seat_spend_disagrees": [r.instance_id for r in rows if r.seat_spend_agrees is False],
        # Censoring, on every arm, by the rule the single arm's driver uses.
        "timeout_s": timeout_s,
        "timeout_censored": [r.instance_id for r in rows if r.timeout_censored],
        "timeout_censored_count": sum(1 for r in rows if r.timeout_censored),
        # The second, independent reading: the clock against the wall the spec
        # set. It is derived and it is here to contradict the first one -- a
        # run past the wall without the reason, or the reason without the run
        # time, means one of the two is not measuring what it is read as.
        "duration_at_or_over_timeout": [
            r.instance_id
            for r in rows
            if timeout_s is not None and r.duration_s is not None and r.duration_s >= timeout_s
        ],
        "timeout_rule_disagreement": (
            [
                r.instance_id
                for r in rows
                if r.duration_s is not None
                and r.timeout_censored != (r.duration_s >= timeout_s)
            ]
            if timeout_s is not None
            else []
        ),
        # And against the single arm's own flag, where the record has one.
        "timeout_flag_disagreement": [
            r.instance_id
            for r in rows
            if r.timeout_recorded is not None and r.timeout_recorded != r.timeout_censored
        ],
        "duration_max_s": max((r.duration_s for r in rows if r.duration_s is not None), default=None),
        "analyst_cards": cards,
        "card_matches_expected": (cards == [expected_card]) if expected_card else None,
        "team_configs": sorted({r.team_config for r in rows if r.team_config}),
        "tokens_total": sum(r.tokens for r in rows),
    }


def _timeout_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The censoring count, worded and derived identically on every arm.

    Printed unconditionally, including when it is zero: "no run was cut off"
    and "this report does not say" were the same blank line on two of the three
    arms, and they are the two whose wall-clock window is the shorter one.
    """
    wall = summary.get("timeout_s")
    lines.append(
        f"cut off at the wall clock: {summary['timeout_censored_count']}/{summary['runs']}"
        + (f" (spec timeout {wall:g}s)" if isinstance(wall, int | float) else " (no spec timeout given)")
        + f" -> {summary['timeout_censored']}"
    )
    disagree = summary.get("timeout_rule_disagreement") or []
    flag_disagree = summary.get("timeout_flag_disagreement") or []
    if disagree or flag_disagree:
        lines.append(
            "  !! timeout readings disagree"
            + (f" -- run time vs recorded reason: {disagree}" if disagree else "")
            + (f" -- recorded flag vs reason: {flag_disagree}" if flag_disagree else "")
        )


def _attempt_note(row: RunRow) -> str:
    """Which attempt this row is, printed only where there was more than one."""
    if row.attempts <= 1:
        return ""
    return f"   [attempt {row.attempt} of {row.attempts} from {row.source_batch}]"


def _retry_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The retry ledger, printed only on a cell that has one."""
    if not summary.get("retried"):
        return
    lines.append(
        f"retried instances: {summary['retried_count']} -> {summary['retried']}"
        f"   (merged from {summary['attempt_sources']})"
    )
    lines.append(
        f"  a later attempt ran: {summary['retry_succeeded_count']} -> {summary['retry_succeeded']}"
    )
    lines.append(
        f"  no attempt ever ran: {summary['infra_failed_count']} -> {summary['infra_failed']}"
    )


def _edges_short(summary: dict[str, Any], lines: list[str]) -> None:
    """The runs that fell short of their own declared edges, named not averaged.

    One run walking none of its six and six runs each missing one are the same
    rate.
    """
    lines.append(
        f"  runs short of their declared edges: {len(summary['edges_unwalked'])}"
        f" -> {[[i, f'{w}/{d}'] for i, w, d in summary['edges_unwalked']]}"
    )


def _edge_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """What the declared topology carried, on an arm that also reports alpha."""
    if summary.get("edges_declared_state") != "declared":
        lines.append(
            "edges declared: none (this arm's runs record no assigned topology,"
            " which is not the same as declaring some and walking none)"
        )
        return
    rate = summary["edges_walked_rate"]
    lines.append(
        f"edges walked {summary['edges_walked']}/{summary['edges_declared']}"
        + (f" = {rate:.3f}" if rate is not None else "")
        + "   (a declared channel that carried at least one message_agent call;"
        " delegation is the line above, and the two are different questions)"
    )
    _edges_short(summary, lines)


def _headroom(row: RunRow) -> str:
    """The tightest seat's remaining allowance, or ``?`` when none was recorded.

    ``?`` and ``0`` are different findings and the column has to keep them
    apart: the first says the ceiling was not in the files, the second says the
    seat was spent to the last token.
    """
    return "?" if row.seat_headroom_min is None else f"{row.seat_headroom_min:,}"


def _cap_lines(summary: dict[str, Any], lines: list[str]) -> None:
    """The three cap counts, worded and split identically on every arm."""
    lines.append(f"seat at budget cap: {len(summary['cap_hit'])}/{summary['runs']} -> {summary['cap_hit']}")
    lines.append(
        f"  stopped by the pre-call reservation: {len(summary['cap_hit_precheck'])}"
        f" -> {summary['cap_hit_precheck']}"
    )
    lines.append(
        f"  overspent after a call returned:     {len(summary['cap_hit_postcall'])}"
        f" -> {summary['cap_hit_postcall']}"
    )
    lines.append(
        f"  stopped at the aggregate ceiling:    {len(summary['cap_hit_aggregate'])}"
        f" -> {summary['cap_hit_aggregate']}"
    )
    lines.append(
        "seat allowance handed to a session (recorded, session_terminal.max_budget_tokens): "
        + (str(summary["seat_cap_tokens"]) or "[]")
        + (
            f"   unknown on {len(summary['seat_cap_unknown'])}/{summary['runs']}"
            if summary["seat_cap_unknown"]
            else ""
        )
    )
    lines.append(
        f"seat budget exhausted (the workflow's own verdict): {len(summary['seat_budget_exhausted'])}"
        f" -> {summary['seat_budget_exhausted']}"
    )
    # Derived, and said so: no record anywhere says what a seat had left when
    # it stopped. A seat that answered with 0.25% of its allowance unspent
    # reads in every other column exactly like one that answered with 90%
    # unspent, and that difference is what the cap columns cannot show.
    tightest = summary["seat_headroom_min"][:3]
    lines.append(
        "smallest seat headroom left (derived = allowance - the role's summed spend), tightest first: "
        + (
            ", ".join(f"{instance} {value:,}" for instance, value in tightest)
            or "n/a (no allowance recorded)"
        )
    )


def _render_single(rows: list[RunRow], summary: dict[str, Any], lines: list[str]) -> str:
    lines.append(
        f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'steps':>5s} {'patch':>6s} "
        f"{'left':>9s} {'cut':>3s} cap"
    )
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {r.steps:>5} {str(r.patch_chars or 0):>6} "
            f"{_headroom(r):>9s} {'CUT' if r.timeout_censored else '-':>3s} "
            + (','.join(r.cap_hit) or '-')
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
            + _attempt_note(r)
        )
    lines.append("")
    lines.append(
        f"valid {summary['valid']}/{summary['runs']}"
        + (f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else "")
        + "   (delivery is a team-arm quantity; none is computed here)"
    )
    lines.append(f"statuses: {summary['statuses']}")
    _edge_lines(summary, lines)
    _retry_lines(summary, lines)
    _cap_lines(summary, lines)
    _timeout_lines(summary, lines)
    lines.append(f"tokens total: {summary['tokens_total']:,}")
    return "\n".join(lines)


def render(rows: list[RunRow], summary: dict[str, Any], missing: list[str]) -> str:
    lines = []
    if missing:
        lines.append(f"MISSING FROM METRICS ({len(missing)}): {missing}")
    # Printed above everything else, because it is a statement about whether
    # the rest of the report can be read at face value: on these runs the seat
    # columns and all three cap counts are zero for want of a file, which is
    # spelt exactly like a run that never hit its cap.
    if summary.get("seat_snapshot_missing"):
        lines.append(
            "!! SEAT SNAPSHOT NOT FOUND for"
            f" {summary['seat_snapshot_missing_count']}/{summary['runs']} runs"
            " -- their seat, delivery and cap columns are zero for want of a file,"
            f" not for want of a stop: {summary['seat_snapshot_missing']}"
        )
    if not summary.get("team", True):
        return _render_single(rows, summary, lines)
    header = (
        f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'analyst':>9s} {'coder':>8s} {'tester':>8s} "
        f"{'left':>9s} {'cut':>3s} {'deleg':5s} {'msgA':>4s} {'aWr':>3s} {'snap':>4s} cap"
    )
    lines.append(header)
    for i, r in enumerate(rows, 1):
        # Summed over the seats that held the role, never the last seat that
        # held it: the workflow arm seats its analyst twice.
        a_tok, c_tok, t_tok = (r.role_tokens.get(k, 0) for k in ("analyst", "coder", "tester"))
        a_writes = sum(s.writes for s in r.seats.values() if s.role == "analyst")
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {a_tok:>9,} {c_tok:>8,} {t_tok:>8,} "
            f"{_headroom(r):>9s} {'CUT' if r.timeout_censored else '-':>3s} "
            f"{'YES' if r.delivered else 'no':5s} {sum(s.msg_agent for s in r.seats.values()):>4} {a_writes:>3} "
            f"{r.tree_snapshots:>4} {','.join(r.cap_hit) or '-'}"
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
            + _attempt_note(r)
        )
    lines.append("")
    excluded = (
        f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else ""
    )
    if summary.get("alpha_readable", summary.get("team", True)):
        lo, hi = summary["ci95"]
        alpha = summary["alpha"]
        lines.append(
            f"delivered {summary['delivered']}/{summary['valid']} valid"
            + (
                f" = {alpha:.3f}   Clopper-Pearson 95% [{lo:.3f}, {hi:.3f}]"
                if alpha is not None
                else ""
            )
            + excluded
        )
        # Beside alpha, never instead of it. The team file declares which role
        # may address which; alpha says whether an agent chose to hand the work
        # on. A cell can walk an edge and deliver nothing, and reading either
        # number off the other is how the two get confused.
        _edge_lines(summary, lines)
    else:
        # No delivery rate on this arm and no interval: its edges are written
        # by a script, so what the runs vary is whether each fixed edge carried
        # anything. That is what gets reported, with the runs that fell short
        # named rather than averaged away.
        rate = summary["edges_walked_rate"]
        lines.append(
            f"edges walked {summary['edges_walked']}/{summary['edges_declared']}"
            + (f" = {rate:.3f}" if rate is not None else "")
            + "   (the topology is script-fixed on this arm, so there is no"
            " delegation rate to read)"
            + excluded
        )
        _edges_short(summary, lines)
    lines.append(f"statuses: {summary['statuses']}")
    _retry_lines(summary, lines)
    _cap_lines(summary, lines)
    _timeout_lines(summary, lines)
    lines.append(
        f"analyst card digests: {summary['analyst_cards']}"
        + (
            ""
            if summary["card_matches_expected"] is None
            else ("  == expected" if summary["card_matches_expected"] else "  != EXPECTED")
        )
    )
    lines.append(f"team configs: {summary['team_configs']}")
    lines.append(f"tokens total: {summary['tokens_total']:,}")
    return "\n".join(lines)


def report_document(rows: list[RunRow], summary: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    return {
        "summary": summary,
        "missing": missing,
        "runs": [
            {
                **{k: v for k, v in asdict(r).items() if k != "seats"},
                "seats": {k: asdict(s) for k, s in r.seats.items()},
            }
            for r in rows
        ],
    }
