"""Read batch metrics and per-role trajectory measurements."""

from __future__ import annotations

import collections
import glob
import json
import os
import re
from typing import Any

from scan_batch_classification import (
    _COMPILED as _COMPILED,
)
from scan_batch_classification import (
    _RULES as _RULES,
)
from scan_batch_classification import (
    _SEAT_COMPILED as _SEAT_COMPILED,
)
from scan_batch_classification import (
    _SEAT_RULES as _SEAT_RULES,
)
from scan_batch_classification import (
    ALPHA_VALID_CLASSES as ALPHA_VALID_CLASSES,
)
from scan_batch_classification import (
    CENSORED_FOR_OUTCOME as CENSORED_FOR_OUTCOME,
)
from scan_batch_classification import (
    KNOWN_STATUSES as KNOWN_STATUSES,
)
from scan_batch_classification import (
    NO_TRAJECTORY as NO_TRAJECTORY,
)
from scan_batch_classification import (
    OUTCOME_VALID_CLASSES as OUTCOME_VALID_CLASSES,
)
from scan_batch_classification import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from scan_batch_classification import (
    SEAT_NATURAL_END_CLASSES as SEAT_NATURAL_END_CLASSES,
)
from scan_batch_classification import (
    SEAT_PREEMPTIVE_CLASSES as SEAT_PREEMPTIVE_CLASSES,
)
from scan_batch_classification import (
    SEAT_UNDETERMINED_CLASSES as SEAT_UNDETERMINED_CLASSES,
)
from scan_batch_classification import (
    UNCLASSIFIED as UNCLASSIFIED,
)
from scan_batch_classification import (
    VALID_STATUSES as VALID_STATUSES,
)
from scan_batch_classification import (
    classify_run as classify_run,
)
from scan_batch_classification import (
    classify_seat_reason as classify_seat_reason,
)

WRITE_TOOLS = frozenset({"apply_patch", "file_write"})

#: Shell fragments that write.  Same regex ``think_scan.py`` used, so the two
#: scanners stay comparable on this column.
BASH_WRITE_RE = re.compile(r"(>\s*[^&\s]|>>|open\([^)]*['\"][wa]|sed -i|tee |cat\s*<<|dd of=)")

#: Teammate-reference terms counted inside aid=0 reasoning text.
TEAMMATE_TERMS = (
    "coder",
    "tester",
    "delegat",
    "teammate",
    "hand off",
    "hand it over",
    "message_agent",
    "team_status",
    "colleague",
)

#: Control terms that must be present if the reasoning text was captured at all.
#: A zero teammate count next to a zero control count means "no reasoning text",
#: not "the model never mentioned its teammates".
REASONING_CONTROL_TERMS = ("test", "patch", "fix")

#: Keys the model's reasoning text has been seen under.
REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


#: How each arm names the per-seat snapshot the runtime autosaves, relative to
#: the directory that holds one run's seats. A team writes
#: ``agent_<aid>_<role>-<hex>.json``; a scripted workflow writes
#: ``<nnn>_<label>.json`` (``000_analyst.json``, ``003_analyst-adjudicate-r1``),
#: which the team pattern does not match. The journal sidecars
#: (``*.json.journal``) match neither, which is why neither has to exclude them.
#: Source for the layouts: /root/git/OpenCollab/opencollab/bootstrap/
#: session_factory.py:99-106 and the DW runs under
#: ``logs-self-collaboration/<instance>/trajectories/*/runtime-*/``.
SEAT_FILE_GLOBS = ("agent_*.json", "[0-9][0-9][0-9]_*.json")

#: The single-agent arm keeps one directory per run at the *batch root*,
#: ``<cell>/agent-<hex>/agent.json``, named by a random id that carries no
#: instance. No glob under ``logs-single/<instance>/`` finds it, which is why
#: every single-arm run read as ``no_trajectory``; the only thing tying the
#: directory to the instance is the run's own ``trajectory_path``.
SINGLE_SEAT_FILE = "agent.json"

#: The role the runtime stamps on every seat of a scripted workflow. A seat
#: that carries it has its identity in its file name instead.
GENERIC_WORKFLOW_ROLE = "workflow_agent"


def _aid_from_filename(path: str) -> int | None:
    """The seat's id from its file name, in either arm's spelling.

    ``agent_<aid>_<role>.json`` (team) and ``<nnn>_<label>.json`` (workflow).
    Source for the layout: /root/git/OpenCollab/opencollab/bootstrap/
    session_factory.py:99-106 (``agent_save_path``).
    """
    base = os.path.basename(path)
    m = re.match(r"agent_(\d+)_", base) or re.match(r"^(\d{3})_", base)
    return int(m.group(1)) if m else None


def _role_from_filename(path: str) -> str | None:
    """A workflow seat's real role, from the name the driver gave its file.

    ``001_coder-r1`` is the coder; ``003_analyst-adjudicate-r1`` is the analyst
    coming back to adjudicate. Without this every DW seat reads as
    ``workflow_agent`` and a per-role column cannot be built at all.
    """
    stem = os.path.basename(path).rsplit(".", 1)[0]
    label = stem.split("_", 1)[1] if "_" in stem else stem
    return label.split("-", 1)[0] or None


def _reasoning_text(message: dict) -> str:
    for key in REASONING_KEYS:
        value = message.get(key)
        if value:
            return str(value)
    return ""


def empty_measurements() -> dict[str, Any]:
    """Zeroed measurement block, so a run with no trajectory still has every key.

    A missing key and a zero must not be the same thing downstream: the run
    carries ``trajectory_found=False`` to say which one this is.
    """
    return {
        "message_agent_calls": 0,
        "message_agent_by_aid": {},
        "team_status_calls": 0,
        "team_status_by_aid": {},
        "write_tool_calls_by_aid": {},
        "bash_write_calls_by_aid": {},
        "tool_calls_by_name": {},
        "tokens_by_aid": {},
        "steps_by_aid": {},
        "assistant_msgs_by_aid": {},
        "roles_by_aid": {},
        # Summed over the seats that held each role, because a scripted
        # workflow seats its analyst twice and a per-aid column cannot be
        # compared with the cell report, which reports per role.
        "tokens_by_role": {},
        "assistant_msgs_by_role": {},
        "seats_by_role": {},
        "seat_phase_by_aid": {},
        "seat_terminal_reason_by_aid": {},
        "seat_terminal_class_by_aid": {},
        "budget_exhausted_aids": [],
        "models": [],
        "reasoning_chars_aid0": 0,
        "reasoning_teammate_hits": 0,
        "reasoning_teammate_hits_by_term": {},
        "reasoning_control_hits": 0,
        "n_agent_files": 0,
    }


def measure_trajectory(runtime_dir: str) -> dict[str, Any]:
    """Per-run mechanism quantities from one ``runtime-*`` directory.

    agent_*.json schema source: /root/git/OpenCollab/opencollab/application/
    session.py:673-700 (``_snapshot_for_save``) -- top level ``aid``, ``role``,
    ``model``, ``session_state`` (``used_tokens``, ``step_count``, ``phase``,
    ``terminal_reason``), plus ``messages`` appended by
    /root/git/OpenCollab/opencollab/adapters/storage.py:129.
    """
    out: dict[str, Any] = empty_measurements()
    calls_by_name: collections.Counter[str] = collections.Counter()
    msg_by_aid: collections.Counter[int] = collections.Counter()
    ts_by_aid: collections.Counter[int] = collections.Counter()
    write_by_aid: collections.Counter[int] = collections.Counter()
    bashw_by_aid: collections.Counter[int] = collections.Counter()
    asst_by_aid: collections.Counter[int] = collections.Counter()
    term_hits: collections.Counter[str] = collections.Counter()
    models: set[str] = set()

    tokens_by_role: collections.Counter[str] = collections.Counter()
    asst_by_role: collections.Counter[str] = collections.Counter()
    seats_by_role: collections.Counter[str] = collections.Counter()

    seat_files: set[str] = set()
    for pattern in SEAT_FILE_GLOBS:
        seat_files.update(glob.glob(os.path.join(runtime_dir, pattern)))
    single = os.path.join(runtime_dir, SINGLE_SEAT_FILE)
    if os.path.isfile(single):
        seat_files.add(single)
    for agent_file in sorted(seat_files):
        try:
            with open(agent_file, encoding="utf-8") as handle:
                agent = json.load(handle)
        except (OSError, ValueError):
            continue
        out["n_agent_files"] += 1
        aid = agent.get("aid")
        if aid is None:
            aid = _aid_from_filename(agent_file)
        if aid is None:
            continue
        aid = int(aid)
        recorded_role = str(agent.get("role") or "")
        role = (
            recorded_role
            if recorded_role and recorded_role != GENERIC_WORKFLOW_ROLE
            else (_role_from_filename(agent_file) or recorded_role)
        )
        if role:
            out["roles_by_aid"][aid] = role
            seats_by_role[role] += 1
        if agent.get("model"):
            models.add(str(agent["model"]))
        state = agent.get("session_state") or {}
        out["tokens_by_aid"][aid] = int(state.get("used_tokens") or 0)
        out["steps_by_aid"][aid] = int(state.get("step_count") or 0)
        if role:
            tokens_by_role[role] += int(state.get("used_tokens") or 0)
        if state.get("phase"):
            out["seat_phase_by_aid"][aid] = state.get("phase")
        if state.get("terminal_reason"):
            out["seat_terminal_reason_by_aid"][aid] = state.get("terminal_reason")
            seat_class = classify_seat_reason(state.get("terminal_reason"))
            if seat_class is not None:
                out["seat_terminal_class_by_aid"][aid] = seat_class
                if seat_class == "budget_exhausted":
                    out["budget_exhausted_aids"].append(aid)

        for message in agent.get("messages") or []:
            if message.get("role") == "assistant":
                asst_by_aid[aid] += 1
                if role:
                    asst_by_role[role] += 1
            reasoning = _reasoning_text(message)
            if reasoning and aid == 0:
                out["reasoning_chars_aid0"] += len(reasoning)
                lowered = reasoning.lower()
                for term in TEAMMATE_TERMS:
                    hits = lowered.count(term)
                    if hits:
                        term_hits[term] += hits
                for term in REASONING_CONTROL_TERMS:
                    out["reasoning_control_hits"] += lowered.count(term)
            for call in message.get("tool_calls") or []:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                calls_by_name[name] += 1
                if name == "message_agent":
                    msg_by_aid[aid] += 1
                elif name == "team_status":
                    ts_by_aid[aid] += 1
                elif name in WRITE_TOOLS:
                    write_by_aid[aid] += 1
                elif name == "bash":
                    raw = fn.get("arguments") or ""
                    command = ""
                    try:
                        command = json.loads(raw).get("command", "") if raw else ""
                    except (ValueError, AttributeError):
                        command = str(raw)
                    if BASH_WRITE_RE.search(str(command)):
                        bashw_by_aid[aid] += 1

    out["message_agent_calls"] = int(sum(msg_by_aid.values()))
    out["message_agent_by_aid"] = dict(sorted(msg_by_aid.items()))
    out["team_status_calls"] = int(sum(ts_by_aid.values()))
    out["team_status_by_aid"] = dict(sorted(ts_by_aid.items()))
    out["write_tool_calls_by_aid"] = dict(sorted(write_by_aid.items()))
    out["bash_write_calls_by_aid"] = dict(sorted(bashw_by_aid.items()))
    out["assistant_msgs_by_aid"] = dict(sorted(asst_by_aid.items()))
    out["tokens_by_role"] = dict(sorted(tokens_by_role.items()))
    out["assistant_msgs_by_role"] = dict(sorted(asst_by_role.items()))
    out["seats_by_role"] = dict(sorted(seats_by_role.items()))
    out["tool_calls_by_name"] = dict(sorted(calls_by_name.items()))
    out["reasoning_teammate_hits_by_term"] = dict(sorted(term_hits.items()))
    out["reasoning_teammate_hits"] = int(sum(term_hits.values()))
    out["budget_exhausted_aids"] = sorted(out["budget_exhausted_aids"])
    out["models"] = sorted(models)
    return out


# =============================================================================
# Cell scanning
# =============================================================================


def _has_seat_snapshot(directory: str) -> bool:
    if os.path.isfile(os.path.join(directory, SINGLE_SEAT_FILE)):
        return True
    return any(glob.glob(os.path.join(directory, pattern)) for pattern in SEAT_FILE_GLOBS)


def _snapshot_dirs(attempt_dir: str) -> set[str]:
    """Find seats within one attempt, including workflow runtime children."""
    directories = {attempt_dir} if _has_seat_snapshot(attempt_dir) else set()
    for child in glob.glob(os.path.join(attempt_dir, "runtime-*")):
        if os.path.isdir(child) and _has_seat_snapshot(child):
            directories.add(child)
    return directories


def _locate_runtime_dir(cell_dir: str, instance_id: str, record: dict | None) -> tuple[str | None, str | None]:
    """Resolve a named attempt first; use legacy discovery only without a path."""
    raw = str((record or {}).get("trajectory_path") or "")
    if raw:
        path = raw.rstrip("/")
        if path.endswith(".jsonl"):
            path = os.path.dirname(path)
        parts = os.path.normpath(path).split(os.sep)
        candidates: set[str] = set()
        if "trajectories" in parts:
            tail = parts[len(parts) - parts[::-1].index("trajectories") :]
            for root in glob.glob(os.path.join(cell_dir, "logs-*", instance_id, "trajectories")):
                candidates.update(_snapshot_dirs(os.path.join(root, *tail)))
        else:
            candidates.update(_snapshot_dirs(os.path.join(cell_dir, os.path.basename(path))))
        if not candidates:
            candidates.update(_snapshot_dirs(path))
        if len(candidates) == 1:
            return next(iter(candidates)), None
        if candidates:
            return None, f"trajectory_path {raw!r} matches multiple seat directories"
        return None, f"trajectory_path {raw!r} has no seat snapshot"

    candidates = set()
    for pattern in SEAT_FILE_GLOBS:
        search = os.path.join(cell_dir, "logs-*", instance_id, "trajectories", "**", pattern)
        for seat in glob.glob(search, recursive=True):
            candidates.add(os.path.dirname(seat))
    if len(candidates) == 1:
        return next(iter(candidates)), None
    if candidates:
        return None, f"instance_id {instance_id!r} has multiple unnamed seat directories"
    return None, None


def _find_runtime_dir(cell_dir: str, instance_id: str, record: dict | None = None) -> str | None:
    """The unique seat directory for this metrics row, when identifiable."""
    return _locate_runtime_dir(cell_dir, instance_id, record)[0]


def _read_metrics(cell_dir: str) -> tuple[list[dict], list[str]]:
    """Every ``metrics.jsonl`` row, plus complaints about unreadable ones.

    The metrics file is the spine on purpose.  Scanning by ``team.json`` instead
    -- which ``think_scan.py`` did -- cannot see a run that died before it wrote
    a trajectory, and an invisible run is exactly a silently shrunk denominator.
    """
    path = os.path.join(cell_dir, "metrics.jsonl")
    rows: list[dict] = []
    problems: list[str] = []
    if not os.path.exists(path):
        return rows, [f"{path}: missing"]
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                problems.append(f"{path}:{lineno}: unparseable ({exc})")
                continue
            record["_source_line"] = lineno
            rows.append(record)
    return rows, problems


def scan_cell(
    cell_dir: str,
    alpha_valid: frozenset[str] = ALPHA_VALID_CLASSES,
    outcome_valid: frozenset[str] = OUTCOME_VALID_CLASSES,
) -> dict[str, Any]:
    """Scan one cell, carrying BOTH denominators.

    ``alpha_valid`` and ``outcome_valid`` are separate on purpose; see the
    ruling above ``ALPHA_VALID_CLASSES``.  Every per-class count is also
    returned, so any third denominator can be recomputed downstream without
    re-running this.
    """
    cell_dir = os.path.abspath(os.path.expanduser(cell_dir))
    name = os.path.basename(cell_dir.rstrip("/"))
    rows, problems = _read_metrics(cell_dir)

    runs: list[dict[str, Any]] = []
    seen: collections.Counter[str] = collections.Counter()
    for record in rows:
        instance_id = record.get("instance_id") or "?"
        seen[instance_id] += 1
        summary = record.get("run_summary") or {}
        status = summary.get("status")
        reason = summary.get("reason")
        error = summary.get("error") if summary.get("error") is not None else record.get("error")
        klass, evidence = classify_run(status, reason, error)

        runtime_dir, location_problem = _locate_runtime_dir(cell_dir, instance_id, record)
        if location_problem:
            problems.append(f"{os.path.join(cell_dir, 'metrics.jsonl')}:{record['_source_line']}: {location_problem}")
        if runtime_dir is None and klass in (alpha_valid | outcome_valid):
            # A run the runtime called complete but that left no trajectory is
            # not a usable observation; say so rather than count it at zero.
            klass, evidence = NO_TRAJECTORY, f"{evidence} (no seat snapshot under {cell_dir})"

        run: dict[str, Any] = {
            "cell": name,
            "cell_path": cell_dir,
            "instance_id": instance_id,
            "metrics_line": record.get("_source_line"),
            "validity_class": klass,
            "valid_for_alpha": klass in alpha_valid,
            "valid_for_outcome": klass in outcome_valid,
            "censored_for_outcome": klass in alpha_valid and klass not in outcome_valid,
            "validity_evidence": evidence,
            "status": status,
            "reason": reason,
            "error": None if error is None else str(error)[:400],
            "steps": summary.get("steps"),
            "tokens": summary.get("tokens"),
            "duration_s": summary.get("duration_s"),
            "patch_chars": record.get("submitted_patch_chars"),
            "trajectory_dir": runtime_dir,
            "trajectory_found": runtime_dir is not None,
        }
        run.update(measure_trajectory(runtime_dir) if runtime_dir else empty_measurements())
        run["delegated"] = run["message_agent_calls"] > 0
        runs.append(run)

    for instance_id, count in seen.items():
        if count > 1:
            problems.append(
                f"{os.path.join(cell_dir, 'metrics.jsonl')}: instance_id {instance_id!r} "
                f"appears {count} times; all {count} rows are kept as separate runs"
            )

    a_valid = [r for r in runs if r["valid_for_alpha"]]
    a_dropped = [r for r in runs if not r["valid_for_alpha"]]
    o_valid = [r for r in runs if r["valid_for_outcome"]]
    o_censored = [r for r in runs if r["censored_for_outcome"]]
    o_dropped = [r for r in runs if not r["valid_for_outcome"] and not r["censored_for_outcome"]]

    def _by_class(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(sorted(collections.Counter(r["validity_class"] for r in rows).items()))

    delegating_alpha = [r for r in a_valid if r["delegated"]]
    delegating_all = [r for r in runs if r["delegated"]]

    # Which seat ran out, aggregated -- see classify_seat_reason.
    seat_budget = collections.Counter()
    for r in runs:
        for aid in r.get("budget_exhausted_aids") or []:
            seat_budget[aid] += 1

    return {
        "cell": name,
        "cell_path": cell_dir,
        "n_records": len(runs),
        # Every class, every time: the raw material for any other denominator.
        "class_counts": _by_class(runs),
        # ---- denominator 1: alpha (budget/truncated runs KEPT) ------------
        "alpha_valid_classes": sorted(alpha_valid),
        "n_valid_alpha": len(a_valid),
        "n_dropped_alpha": len(a_dropped),
        "dropped_alpha_by_class": _by_class(a_dropped),
        "dropped_alpha_runs": [
            {
                "instance_id": r["instance_id"],
                "metrics_line": r["metrics_line"],
                "validity_class": r["validity_class"],
                "validity_evidence": r["validity_evidence"],
            }
            for r in a_dropped
        ],
        "alpha_num": len(delegating_alpha),
        "alpha_den": len(a_valid),
        "alpha": (len(delegating_alpha) / len(a_valid)) if a_valid else None,
        # alpha the old way (every metrics row in the denominator), printed
        # beside it so a changed cell is visible instead of silently corrected.
        "alpha_all_num": len(delegating_all),
        "alpha_all_den": len(runs),
        "alpha_all": (len(delegating_all) / len(runs)) if runs else None,
        # ---- denominator 2: resolve rate / cost (uncensored only) ---------
        "outcome_valid_classes": sorted(outcome_valid),
        "n_valid_outcome": len(o_valid),
        "n_censored_outcome": len(o_censored),
        "censored_outcome_by_class": _by_class(o_censored),
        "censored_outcome_runs": [
            {
                "instance_id": r["instance_id"],
                "metrics_line": r["metrics_line"],
                "validity_class": r["validity_class"],
                "tokens": r["tokens"],
                "patch_chars": r["patch_chars"],
            }
            for r in o_censored
        ],
        "n_dropped_outcome": len(o_dropped),
        "dropped_outcome_by_class": _by_class(o_dropped),
        "tokens_outcome": [r["tokens"] for r in o_valid],
        "patch_chars_outcome": [r["patch_chars"] for r in o_valid],
        # The censored side kept separately, never merged into the line above.
        "tokens_censored": [r["tokens"] for r in o_censored],
        "patch_chars_censored": [r["patch_chars"] for r in o_censored],
        # ---- mechanism quantities, on the alpha denominator ---------------
        "message_agent_calls_alpha": sum(r["message_agent_calls"] for r in a_valid),
        "team_status_calls_alpha": sum(r["team_status_calls"] for r in a_valid),
        "reasoning_chars_aid0_alpha": [r["reasoning_chars_aid0"] for r in a_valid],
        "reasoning_teammate_hits_alpha": sum(r["reasoning_teammate_hits"] for r in a_valid),
        "reasoning_control_hits_alpha": sum(r["reasoning_control_hits"] for r in a_valid),
        "budget_exhausted_seat_counts": dict(sorted(seat_budget.items())),
        "problems": problems,
        "runs": runs,
    }


# =============================================================================
# Reporting
# =============================================================================
