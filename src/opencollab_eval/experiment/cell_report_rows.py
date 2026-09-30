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

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencollab_eval.experiment.cell_report_messaging import (
    MESSAGE_REFUSED_EVENT,
    MESSAGE_SENT_EVENT,
    MESSAGE_TOOL,
    event_target,
    message_calls,
    role_key,
    sent_targets,
)
from opencollab_eval.experiment.cell_report_metric_order import report_metric_records

WRITE_TOOLS = frozenset({"apply_patch", "file_write"})
#: The delegate seats of the handoff roster, used only when a run recorded no
#: ``assigned.topology_nodes`` row to derive them from. Every team run since
#: that row existed derives them; this is what the runs written before it get.
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
#: Why a paid run of a pre-registered instance leaves every denominator. The
#: instance's SWE-bench evaluation environment does not run -- the benchmark's
#: own gold patch does not resolve there -- so the run has no outcome to score,
#: whatever the agent did. Formatted with the replacement's instance id.
REPLACED_REASON = "eval environment unusable: replaced by {instance} per ordered draw"
#: Why a stand-in's run leaves every denominator in its turn. The replacement
#: that put it in the cell is withdrawn, because the fault that triggered the
#: replacement was not the instance's -- so the instance it stood in for is
#: back, and this run, paid for and counted by nothing, is kept here rather
#: than deleted. Formatted with the instance the run stood in for and the
#: reason the cell's spec gives for withdrawing.
WITHDRAWN_REASON = "replacement withdrawn: this run stood in for {instance}, which is back in the cell. {why}"
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
#: The row that names the *seats*: ``entry_role``, ``declared_roles``,
#: ``turns_serialized`` and one ``{aid, role, entry, tools, ...}`` per node.
#: Read for one thing only -- which seats are not the entry seat -- because
#: naming those by their literal role names is a reader of one roster: on a
#: team of an Adopter and two Coders the names ``coder``/``tester`` match
#: nothing, alpha reads 0.000, and a Clopper-Pearson interval is printed
#: around it with no complaint.
ASSIGNED_NODES_EVENT = "assigned.topology_nodes"
#: The tool one seat addresses another with. A declared edge is *walked* when a
#: seat holding its ``from_role`` successfully queued at least one message to
#: its ``to_role``. That is not alpha: alpha is whether an agent
#: chose to hand the work on (a coder or tester seat that spent tokens and
#: spoke), one number per run; walking an edge is whether a declared channel
#: carried anything at all. A run can walk an edge and deliver nothing.


@dataclass
class Seat:
    role: str
    tokens: int = 0
    steps: int = 0
    assistant: int = 0
    writes: int = 0
    msg_agent: int = 0
    msg_agent_sent: int = 0
    terminal: str = ""
    #: Successfully queued targets from scheduler events or matched tool receipts.
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
    record_id: str = ""
    patch_sha256: str = ""
    # Recorded token consumption across the attempts from which this row was selected.
    attempt_tokens_total: int | None = None
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
    #: Every declared delegate seat spent tokens and spoke, not merely one of
    #: them. On the handoff roster ``delivered`` and this differ when the
    #: Analyst used the Coder and never the Tester; on the two-candidate
    #: roster the difference is the whole point, because a card that says "ask
    #: the first, then write the second its own brief" is carried out only if
    #: both Coders ran. One candidate is the failure that matters there, and
    #: ``delivered`` reads it as adherence.
    every_delegate: bool = False
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
    #: Whether the analyst had already changed the source when the script
    #: probed the tree between the analyze and implement phases
    #: (``workflow_result.analyst_wrote_source``). It is the same question the
    #: team arm asks as adherence, asked of an arm whose sequence is not the
    #: model's to choose, and after the coder has run no later probe can answer
    #: it. ``None`` on every arm that never probes -- which is not the same
    #: finding as an arm that probed and found nothing.
    analyst_wrote_source: bool | None = None
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
    #: The pre-registered instance this run stands in for, when it is a
    #: replacement. Empty on every run of the drawn slice itself. A replacement
    #: is not a second attempt at the same task -- the task changed -- so it is
    #: a field of its own rather than a larger ``attempt``.
    replacement_for: str = ""

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


#: Each of these arms gives every generation attempt its own anonymous task
#: directory below ``trajectories``. A trace can live in the task directory
#: while its role snapshots live in a runtime child, or both can live in the
#: runtime child named by the trace. Either layout stays within the named run.
#: Best-of-N retains multiple candidates for one run and keeps its own layout.
ATTEMPT_SCOPED_ARMS = frozenset({"team", "self-collaboration", "self-collaboration-reading-analyst"})


def _attempt_dir(root: Path, record: dict[str, Any] | None) -> Path | None:
    """The trace directory the record names, under the pulled ``root``, if present.

    ``trajectory_path`` was written on the machine that ran the batch. Its
    tail below ``trajectories/`` identifies the same directory in a pulled copy.
    """
    raw = str((record or {}).get("trajectory_path") or "")
    if not raw:
        return None
    path = Path(raw)
    if path.suffix == ".jsonl":
        path = path.parent
    parts = path.parts
    if "trajectories" not in parts:
        return None
    tail = parts[len(parts) - parts[::-1].index("trajectories") :]
    if not tail or tail[0] in {".", ".."} or ".." in tail:
        return None
    directory = root.joinpath(*tail)
    return directory if directory.is_dir() else None


def _agent_files(cell: Path, arm: str, instance_id: str, record: dict[str, Any] | None = None) -> list[Path]:
    """Per-seat snapshots for one run, for the arms that write them per instance."""
    root = cell / f"logs-{arm}" / instance_id / "trajectories"
    if not root.exists():
        return []
    found: set[Path] = set()
    if arm in ATTEMPT_SCOPED_ARMS:
        if (record or {}).get("trajectory_path"):
            attempt = _attempt_dir(root, record)
        else:
            # Older metrics lack an attempt path. A single task directory has
            # an unambiguous source; multiple tasks cannot be assigned to a row.
            tasks = [path for path in root.iterdir() if path.is_dir()]
            attempt = tasks[0] if len(tasks) == 1 else None
        if attempt is None:
            return []
        for pattern in _SEAT_FILE_PATTERNS:
            found.update(attempt.glob(pattern.rsplit("/", 1)[-1]))
            found.update(attempt.glob(pattern.partition("/")[2]))
        # A task-level trace or pathless legacy record may span several runtime
        # snapshots. Only one seat directory can be assigned to this row.
        if len({path.parent for path in found}) != 1:
            return []
        return sorted(found)
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
    return _agent_files(cell, arm, record["instance_id"], record)


def _int_or_none(value: Any) -> int | None:
    """An integer when the record holds one, else ``None`` -- never ``0``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _event_log_facts(
    seat_paths: list[Path],
) -> tuple[
    dict[str, dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None, dict[str, list[dict[str, Any]]]
]:
    """Read the run's session, topology, roster, and message decisions.

    Returns the ``session_terminal`` payload of each session keyed by ``aid``,
    the single ``assigned.topology_edges`` payload and the single
    ``assigned.topology_nodes`` payload, and successful decisions per sender.
    The decision payloads retain their resolved aids and message fields for
    pairing with queue receipts in the seat snapshots. Truncated or missing
    event records leave those receipts available to the report.

    The logs run to hundreds of megabytes a batch and hold a handful of these
    rows, so a line that can contain neither is skipped before it is parsed.
    """
    found: dict[str, dict[str, Any]] = {}
    topology: dict[str, Any] | None = None
    nodes: dict[str, Any] | None = None
    messages: dict[str, list[dict[str, Any]]] = {}
    seen_messages: set[str] = set()
    for directory in dict.fromkeys(path.parent for path in seat_paths):
        for name in EVENT_LOG_NAMES:
            log = directory / name
            if not log.is_file():
                continue
            try:
                with log.open(encoding="utf-8") as handle:
                    for line in handle:
                        if (
                            SESSION_TERMINAL_EVENT not in line
                            and ASSIGNED_TOPOLOGY_EVENT not in line
                            and ASSIGNED_NODES_EVENT not in line
                            and MESSAGE_SENT_EVENT not in line
                            and MESSAGE_REFUSED_EVENT not in line
                        ):
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
                        elif kind == ASSIGNED_NODES_EVENT:
                            nodes = payload
                        elif kind in {MESSAGE_SENT_EVENT, MESSAGE_REFUSED_EVENT}:
                            sender = payload.get("from_aid")
                            if not isinstance(sender, int) or isinstance(sender, bool):
                                continue
                            if kind != MESSAGE_SENT_EVENT:
                                continue
                            target = event_target(payload)
                            message_id = payload.get("message_id")
                            if target and (not message_id or str(message_id) not in seen_messages):
                                messages.setdefault(str(sender), []).append(payload)
                                if message_id:
                                    seen_messages.add(str(message_id))
            except OSError:
                continue
    return found, topology, nodes, messages


def delegate_roles(nodes: dict[str, Any] | None) -> frozenset[str] | None:
    """The roles of this run's non-entry seats, or ``None`` when it named none.

    Alpha is whether the entry agent *chose* to hand the work on, so the seats
    it could hand it to are every seat but its own. Reading them off the run's
    own node list makes that roster-independent: the literal pair
    ``{"coder", "tester"}`` is the handoff team's answer, and on any other team
    it matches nothing, which reads exactly like an entry agent that delegated
    nothing -- alpha 0.000, with an interval printed around it.

    ``None`` rather than an empty set when the row is missing or malformed, so
    the caller can fall back instead of concluding that a run had no delegates.
    """
    if not nodes:
        return None
    listed = nodes.get("nodes")
    if not isinstance(listed, list):
        return None
    roles = {
        str(node.get("role"))
        for node in listed
        if isinstance(node, dict) and node.get("role") and not node.get("entry")
    }
    # A node list with no entry seat marked would make every seat a delegate,
    # which is the same shape of error in the other direction.
    if not roles or not any(isinstance(n, dict) and n.get("entry") for n in listed):
        return None
    return frozenset(roles)


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

    Targets come from successful sends. Receipt-only snapshots resolve the
    acknowledged aid against the run's roster, while current scheduler events
    carry the resolved role directly.
    """
    role_of_aid = {aid: seat.role for aid, seat in seats.items()}
    edge_keys = {(role_key(source), role_key(target)): (source, target) for source, target in declared}
    walked: set[tuple[str, str]] = set()
    for seat in seats.values():
        for target in seat.msg_agent_targets:
            kind, _, value = target.partition(":")
            to_role = value if kind == "role" else role_of_aid.get(value)
            edge = edge_keys.get((role_key(seat.role), role_key(to_role))) if to_role else None
            if edge:
                walked.add(edge)
    return walked


def _every_delegate_worked(seats: dict[str, Any], nodes: dict[str, Any] | None) -> bool:
    """Did every declared delegate seat spend tokens and speak?

    ``delivered`` asks whether the entry agent handed work to *someone*, which
    is the right question for a roster whose delegates are interchangeable. It
    is the wrong one for a roster that declares two candidate seats and a card
    that asks for both: a run that used one Coder and never addressed the other
    produced one candidate, not two, and reads as adherent.

    A seat that was declared and never seated counts as not having worked --
    that is the failure this quantity exists to catch -- so the roles come from
    the run's declared nodes rather than from the seats that happen to exist.
    Returns ``False`` when the run declared no delegates at all, because there
    is then nothing this could be true of.
    """
    declared = delegate_roles(nodes)
    if declared is None:
        declared = frozenset(s.role for s in seats.values() if s.role in DELEGATE_ROLES)
    if not declared:
        return False
    worked = {s.role for s in seats.values() if s.tokens > 0 and s.assistant > 0}
    return declared <= worked


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


def _read_seat(
    path: Path, message_events: dict[str, list[dict[str, Any]]] | None = None
) -> tuple[int, Seat]:
    data = json.loads(path.read_text(encoding="utf-8"))
    aid = int(data.get("aid") or 0)
    state = data.get("session_state") or {}
    messages = data.get("messages") or []
    calls, receipts = message_calls(messages)
    targets = sent_targets(receipts, (message_events or {}).get(str(aid), []))
    seat = Seat(
        role=_seat_role(path, str(data.get("role") or "")),
        tokens=int(state.get("used_tokens") or 0),
        steps=int(state.get("step_count") or 0),
        assistant=sum(1 for m in messages if m.get("role") == "assistant"),
        writes=sum(1 for name in calls if name in WRITE_TOOLS),
        msg_agent=sum(1 for name in calls if name == MESSAGE_TOOL),
        msg_agent_sent=len(targets),
        terminal=str(state.get("terminal_reason") or ""),
        msg_agent_targets=targets,
    )
    return aid, seat


def run_rows(cell: str | Path, arm: str = "team") -> list[RunRow]:
    cell = Path(cell)
    metrics = cell / "metrics.jsonl"
    if not metrics.exists():
        raise FileNotFoundError(f"{metrics} not found; pull the batch first")
    rows: list[RunRow] = []
    for record in report_metric_records(cell, metrics, arm):
        summary = record.get("run_summary") or {}
        # A workflow arm records its seat boundaries, its seat ledger and
        # its own stop verdict inside its own result rather than at the top
        # level (see ``_result_metrics``), so a DW run read only at the top
        # level shows zero snapshots -- the same reading an arm that records
        # none produces.
        workflow_result = record.get("workflow_result")
        seat_paths = _seat_files(cell, arm, record)
        terminals, topology, nodes, messages = _event_log_facts(seat_paths)
        seats: dict[str, Seat] = {}
        for path in seat_paths:
            aid, seat = _read_seat(path, messages)
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
        analyst_wrote_source: bool | None = None
        if isinstance(workflow_result, dict):
            raw_spend = workflow_result.get("seat_spend")
            if isinstance(raw_spend, dict):
                recorded_spend = {str(k): int(v or 0) for k, v in raw_spend.items()}
            # The scripted workflow sets its own seat allowance and records
            # it; ``exhausted()`` (self_collaboration.py:466-467) is what
            # writes the verdict below when a round stops for want of one.
            seat_cap = _int_or_none(workflow_result.get("seat_cap"))
            probed = workflow_result.get("analyst_wrote_source")
            analyst_wrote_source = probed if isinstance(probed, bool) else None
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
            {role: seat_cap - spent for role, spent in role_tokens.items()} if seat_cap is not None else {}
        )
        row = RunRow(
            instance_id=record["instance_id"],
            status=str(summary.get("status") or record.get("runtime_status") or ""),
            reason=str(summary.get("reason") or ""),
            tokens=int(summary.get("tokens") or record.get("tokens_used") or 0),
            steps=int(summary.get("steps") or 0),
            record_id=str(record.get("record_id") or ""),
            patch_sha256=str(record.get("patch_sha256") or ""),
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
            delivered=any(
                s.role in (delegate_roles(nodes) or DELEGATE_ROLES) and s.tokens > 0 and s.assistant > 0
                for s in seats.values()
            ),
            every_delegate=_every_delegate_worked(seats, nodes),
            tree_snapshots=len(snapshots or []),
            cap_hit=[aid for aid, s in seats.items() if "budget" in s.terminal.lower()],
            cap_hit_precheck=[
                aid
                for aid, s in seats.items()
                if CAP_PRECHECK_MARKER in s.terminal or s.terminal.startswith(CAP_PRECHECK_SPENT_PREFIX)
            ],
            cap_hit_postcall=[aid for aid, s in seats.items() if CAP_POSTCALL_MARKER in s.terminal],
            cap_hit_aggregate=[aid for aid, s in seats.items() if s.terminal.startswith(CAP_AGGREGATE_PREFIX)],
            seat_cap=seat_cap,
            role_headroom=role_headroom,
            seat_headroom_min=min(role_headroom.values()) if role_headroom else None,
            budget_exhausted=budget_exhausted,
            edges_walked=edges_walked,
            edges_declared=edges_declared,
            analyst_wrote_source=analyst_wrote_source,
            duration_s=(
                float(summary["duration_s"]) if isinstance(summary.get("duration_s"), int | float) else None
            ),
            # ``gen_prediction_agent.py:102`` verbatim. The workflow and
            # team paths write the same pair into ``run_summary`` from
            # ``runtime_status``/``runtime_reason``
            # (gen_prediction_workflow.py:184-203), so this one rule reads
            # all three arms off the block they share.
            timeout_censored=(
                str(summary.get("status") or "") == "stopped" and str(summary.get("reason") or "") == "timeout"
            ),
            timeout_recorded=(bool(record["wall_clock_timeout"]) if "wall_clock_timeout" in record else None),
            patch_chars=record.get("submitted_patch_chars"),
            card=(record.get("role_prompt_sha256") or {}).get("analyst"),
            team_config=record.get("team_config_path"),
            seat_snapshot_found=bool(seats),
        )
        rows.append(row)
    return rows
