"""What one cell's runs did, read from the files the driver wrote.

Per run: status and tokens from ``metrics.jsonl``; per seat, tokens, steps,
assistant turns, ``message_agent`` calls and write-tool calls from the agent
files the team runtime leaves under ``logs-<arm>/<instance>/trajectories``.
"Delivered" is the delegation criterion the ladder is read on: a coder or
tester seat that spent tokens *and* produced at least one assistant turn.

The denominator is stated, not assumed: runs that failed before any model step
are listed and excluded; a run stopped at its seat cap is valid. The interval
is Clopper-Pearson, so 0 of n and n of n get honest bounds.
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


@dataclass
class RunRow:
    instance_id: str
    status: str
    reason: str
    tokens: int
    steps: int
    seats: dict[str, Seat] = field(default_factory=dict)
    delivered: bool = False
    tree_snapshots: int = 0
    cap_hit: list[str] = field(default_factory=list)
    cap_hit_precheck: list[str] = field(default_factory=list)
    cap_hit_postcall: list[str] = field(default_factory=list)
    patch_chars: int | None = None
    card: str | None = None
    team_config: str | None = None

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


def _agent_files(cell: Path, arm: str, instance_id: str) -> list[Path]:
    """Per-seat snapshots for one run, whichever arm wrote them.

    The single-agent arm is not readable here: it writes
    ``<cell>/agent-<hex>/agent.json`` at the batch root, and that path carries
    no instance id, so a row cannot be attributed to a run from it. Delivery is
    undefined on that arm anyway (``summarize(team=False)``).
    """
    root = cell / f"logs-{arm}" / instance_id / "trajectories"
    if not root.exists():
        return []
    found: set[Path] = set()
    for pattern in _SEAT_FILE_PATTERNS:
        found.update(root.glob(pattern))
    return sorted(found)


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
    calls = [
        ((call.get("function") or {}).get("name") or "")
        for message in messages
        for call in (message.get("tool_calls") or [])
    ]
    seat = Seat(
        role=_seat_role(path, str(data.get("role") or "")),
        tokens=int(state.get("used_tokens") or 0),
        steps=int(state.get("step_count") or 0),
        assistant=sum(1 for m in messages if m.get("role") == "assistant"),
        writes=sum(1 for name in calls if name in WRITE_TOOLS),
        msg_agent=sum(1 for name in calls if name == "message_agent"),
        terminal=str(state.get("terminal_reason") or ""),
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
            seats: dict[str, Seat] = {}
            for path in _agent_files(cell, arm, record["instance_id"]):
                aid, seat = _read_seat(path)
                seats[str(aid)] = seat
            # A workflow arm records its seat boundaries inside its own result
            # rather than at the top level (see ``_result_metrics``), so a DW
            # run read only at the top level shows zero snapshots -- the same
            # reading an arm that records none produces.
            workflow_result = record.get("workflow_result")
            snapshots = record.get("tree_snapshots")
            if not snapshots and isinstance(workflow_result, dict):
                snapshots = workflow_result.get("tree_snapshots")
            row = RunRow(
                instance_id=record["instance_id"],
                status=str(summary.get("status") or record.get("runtime_status") or ""),
                reason=str(summary.get("reason") or ""),
                tokens=int(summary.get("tokens") or record.get("tokens_used") or 0),
                steps=int(summary.get("steps") or 0),
                seats=seats,
                delivered=any(s.role in DELEGATE_ROLES and s.tokens > 0 and s.assistant > 0 for s in seats.values()),
                tree_snapshots=len(snapshots or []),
                cap_hit=[aid for aid, s in seats.items() if "budget" in s.terminal.lower()],
                cap_hit_precheck=[
                    aid for aid, s in seats.items() if CAP_PRECHECK_MARKER in s.terminal
                ],
                cap_hit_postcall=[
                    aid for aid, s in seats.items() if CAP_POSTCALL_MARKER in s.terminal
                ],
                patch_chars=record.get("submitted_patch_chars"),
                card=(record.get("role_prompt_sha256") or {}).get("analyst"),
                team_config=record.get("team_config_path"),
            )
            rows.append(row)
    return rows


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


def summarize(rows: list[RunRow], expected_card: str | None = None, team: bool = True) -> dict[str, Any]:
    """Counts over the cell. Delivery and its interval exist only for a team arm:
    a single agent has nobody to deliver to, so the quantity is undefined there,
    not zero."""
    valid = [r for r in rows if r.valid]
    delivered = sum(1 for r in valid if r.delivered) if team else None
    low, high = clopper_pearson(delivered, len(valid)) if team else (None, None)
    cards = sorted({r.card for r in rows if r.card})
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    return {
        "runs": len(rows),
        "valid": len(valid),
        "invalid": [r.instance_id for r in rows if not r.valid],
        "team": team,
        "delivered": delivered,
        "alpha": (delivered / len(valid)) if (team and valid) else None,
        "ci95": [low, high] if team else None,
        "statuses": statuses,
        "cap_hit": [r.instance_id for r in rows if r.cap_hit],
        # The two halves of ``cap_hit``, kept apart because only the second is
        # a run whose outcome the budget chose.
        "cap_hit_precheck": [r.instance_id for r in rows if r.cap_hit_precheck],
        "cap_hit_postcall": [r.instance_id for r in rows if r.cap_hit_postcall],
        "analyst_cards": cards,
        "card_matches_expected": (cards == [expected_card]) if expected_card else None,
        "team_configs": sorted({r.team_config for r in rows if r.team_config}),
        "tokens_total": sum(r.tokens for r in rows),
    }


def _render_single(rows: list[RunRow], summary: dict[str, Any], lines: list[str]) -> str:
    lines.append(f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'steps':>5s} {'patch':>6s}")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {r.steps:>5} {str(r.patch_chars or 0):>6}"
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
        )
    lines.append("")
    lines.append(
        f"valid {summary['valid']}/{summary['runs']}"
        + (f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else "")
        + "   (delivery is a team-arm quantity; none is computed here)"
    )
    lines.append(f"statuses: {summary['statuses']}")
    lines.append(f"tokens total: {summary['tokens_total']:,}")
    return "\n".join(lines)


def render(rows: list[RunRow], summary: dict[str, Any], missing: list[str]) -> str:
    lines = []
    if missing:
        lines.append(f"MISSING FROM METRICS ({len(missing)}): {missing}")
    if not summary.get("team", True):
        return _render_single(rows, summary, lines)
    header = (
        f"{'#':>3} {'instance_id':40s} {'status':10s} {'tok':>9s} {'analyst':>9s} {'coder':>8s} {'tester':>8s} "
        f"{'deleg':5s} {'msgA':>4s} {'aWr':>3s} {'snap':>4s} cap"
    )
    lines.append(header)
    for i, r in enumerate(rows, 1):
        by_role = {s.role: s for s in r.seats.values()}
        a, c, t = (by_role.get(k, Seat(k)) for k in ("analyst", "coder", "tester"))
        lines.append(
            f"{i:>3} {r.instance_id:40s} {r.status:10s} {r.tokens:>9,} {a.tokens:>9,} {c.tokens:>8,} {t.tokens:>8,} "
            f"{'YES' if r.delivered else 'no':5s} {sum(s.msg_agent for s in r.seats.values()):>4} {a.writes:>3} "
            f"{r.tree_snapshots:>4} {','.join(r.cap_hit) or '-'}"
            + ("" if r.valid else "   [excluded: " + (r.reason or r.status) + "]")
        )
    lo, hi = summary["ci95"]
    alpha = summary["alpha"]
    lines.append("")
    lines.append(
        f"delivered {summary['delivered']}/{summary['valid']} valid"
        + (f" = {alpha:.3f}   Clopper-Pearson 95% [{lo:.3f}, {hi:.3f}]" if alpha is not None else "")
        + (f"   excluded {len(summary['invalid'])}: {summary['invalid']}" if summary["invalid"] else "")
    )
    lines.append(f"statuses: {summary['statuses']}")
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
