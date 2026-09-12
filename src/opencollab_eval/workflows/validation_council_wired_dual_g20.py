"""Two exact isolated G20 candidates with a public-red mechanical switch."""

from __future__ import annotations

import copy
from typing import Any

from opencollab.workflows import CandidateRun, workflow

from ._public_api import toolset
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    structured_role_timeout_seconds,
)
from .validation_council_solve import validation_council_solve
from .validation_council_wiring_ablation import _WiringContext

CANDIDATE_G20_BUDGET = 3_200_000
TOTAL_RUNNER_BUDGET = 2 * CANDIDATE_G20_BUDGET
PUBLIC_PROBE_BUDGET = 180_000
MAX_PUBLIC_COMMAND_BYTES = 4_000
MAX_PUBLIC_RECORDS = 8

PUBLIC_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "commands_run": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "commands_run"],
    "additionalProperties": False,
}

PUBLIC_PROBE_PROMPT = """\
You are the read-only public test executor after one exact G20 workflow. You
cannot modify the candidate worktree.

{rules}

Issue
{goal}

Shared public command selected from candidate A
{public_command}

Inspect the live diff and directly affected source. Translate the shared public
command into the corresponding run_tests target and runner, then execute it.
When the exact command cannot be represented by run_tests, do not substitute an
easier target. Report only commands actually executed. Do not use official
results, hidden tests, FAIL_TO_PASS identifiers, or grader patches."""


def _clip_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    return payload[:limit].decode("utf-8", errors="ignore")


def _probe_tools() -> list[Any]:
    return toolset("file_read", "run_tests", "grep", "git_diff")


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": _clip_text(record.get("target"), 2_000),
        "runner": _clip_text(record.get("runner"), 200),
        "command": _clip_text(record.get("command"), MAX_PUBLIC_COMMAND_BYTES),
        "exit_code": record.get("exit_code"),
        "verified": record.get("verified") is True,
    }


def _verification_records(tools: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for tool in tools:
        for record in getattr(tool, "verification_records", ()):
            if isinstance(record, dict):
                records.append(_public_record(record))
    return records[:MAX_PUBLIC_RECORDS]


def _first_public_command(cartography: Any) -> str:
    if not isinstance(cartography, dict):
        return ""
    commands = cartography.get("runner_commands")
    if not isinstance(commands, list):
        return ""
    for command in commands:
        normalized = str(command or "").strip()
        if normalized:
            return _clip_text(normalized, MAX_PUBLIC_COMMAND_BYTES)
    return ""


async def _run_public_probe(
    ctx: Any,
    *,
    goal: str,
    public_command: str,
) -> tuple[Any, list[dict[str, Any]]]:
    if not public_command:
        return None, []
    tools = _probe_tools()
    try:
        output = await ctx.agent(
            PUBLIC_PROBE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                public_command=public_command,
            ),
            schema=PUBLIC_PROBE_SCHEMA,
            label="dual-g20-public-probe",
            tools=tools,
            budget=PUBLIC_PROBE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"dual G20 public probe unavailable after {type(exc).__name__}")
        output = None
    return output, _verification_records(tools)


async def _exact_g20_candidate_workflow(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run the exact G20 role graph, then one read-only public probe."""
    wired = _WiringContext(ctx)
    g20_result = await validation_council_solve(wired, args)
    forced_command = str(args.get("dual_g20_public_command") or "").strip()
    cartography = copy.deepcopy(wired._records.get("test-cartographer"))
    public_command = forced_command or _first_public_command(cartography)
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    probe, records = await _run_public_probe(
        ctx,
        goal=goal,
        public_command=public_command,
    )
    if records:
        public_command = str(records[0].get("command") or public_command)
    return {
        "g20_result": g20_result,
        "public_command": public_command,
        "public_probe": probe,
        "public_test_records": records,
    }


async def _candidate(
    ctx: Any,
    *,
    label: str,
    args: dict[str, Any],
) -> CandidateRun:
    try:
        return await ctx.candidate_workflow(
            _exact_g20_candidate_workflow,
            args,
            label=label,
            budget=CANDIDATE_G20_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"{label} unavailable after {type(exc).__name__}")
        return CandidateRun(
            label=label,
            output=None,
            diff="",
            test_records=(),
            verified_targets=(),
        )


def _candidate_output(candidate: CandidateRun) -> dict[str, Any]:
    return candidate.output if isinstance(candidate.output, dict) else {}


def _candidate_command(candidate: CandidateRun) -> str:
    return str(_candidate_output(candidate).get("public_command") or "").strip()


def _candidate_records(candidate: CandidateRun) -> list[dict[str, Any]]:
    records = _candidate_output(candidate).get("public_test_records")
    if not isinstance(records, list):
        return []
    return [_public_record(record) for record in records[:MAX_PUBLIC_RECORDS] if isinstance(record, dict)]


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("target") or "").strip(),
        str(record.get("runner") or "").strip(),
        str(record.get("command") or "").strip(),
    )


def _record_state(record: dict[str, Any]) -> str | None:
    exit_code = record.get("exit_code")
    if not all(_record_key(record)) or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    if exit_code == 0 and record.get("verified") is True:
        return "green"
    if exit_code != 0 and record.get("verified") is False:
        return "red"
    return None


def _public_red_winner(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> str | None:
    states_a = {
        _record_key(record): state
        for record in _candidate_records(candidate_a)
        if (state := _record_state(record)) is not None
    }
    states_b = {
        _record_key(record): state
        for record in _candidate_records(candidate_b)
        if (state := _record_state(record)) is not None
    }
    decisions = {
        "A" if states_a[key] == "green" else "B"
        for key in states_a.keys() & states_b.keys()
        if {states_a[key], states_b[key]} == {"green", "red"}
    }
    return next(iter(decisions)) if len(decisions) == 1 else None


def _choose(candidate_a: CandidateRun, candidate_b: CandidateRun) -> tuple[str | None, str]:
    nonempty_a = bool(candidate_a.diff.strip())
    nonempty_b = bool(candidate_b.diff.strip())
    if nonempty_a and not nonempty_b:
        return "A", "only-a-nonempty"
    if nonempty_b and not nonempty_a:
        return "B", "only-b-nonempty"
    if not nonempty_a and not nonempty_b:
        return None, "both-empty"
    if candidate_a.diff == candidate_b.diff:
        return "A", "identical-diff"
    public_winner = _public_red_winner(candidate_a, candidate_b)
    if public_winner is not None:
        return public_winner, "same-command-public-red"
    return "A", "default-a"


async def _adopt(
    ctx: Any,
    *,
    winner: str | None,
    candidates: dict[str, CandidateRun],
    preserve_paths: list[str],
) -> tuple[str | None, list[str]]:
    if winner is None:
        return None, []
    order = [winner, *(label for label in ("A", "B") if label != winner)]
    attempts: list[str] = []
    for label in order:
        candidate = candidates[label]
        if not candidate.diff.strip():
            continue
        attempts.append(label)
        try:
            await ctx.adopt_candidate(candidate, preserve_paths=preserve_paths)
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"dual G20 adoption failed ({label}) after {type(exc).__name__}")
            continue
        return label, attempts
    return None, attempts


@workflow(
    name="validation-council-wired-dual-g20-v1",
    description="Two exact isolated G20 candidates with public-red switching",
    phases=["candidate-a", "candidate-b", "mechanical-selection", "adoption"],
)
async def validation_council_wired_dual_g20_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Keep candidate A unless exact shared public evidence supports B."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("dual-g20-candidate-a")
    candidate_a = await _candidate(
        ctx,
        label="dual-g20-a",
        args=dict(args),
    )
    shared_command = _candidate_command(candidate_a)

    await ctx.phase("dual-g20-candidate-b")
    candidate_b_args = dict(args)
    candidate_b_args["dual_g20_public_command"] = shared_command
    candidate_b = await _candidate(
        ctx,
        label="dual-g20-b",
        args=candidate_b_args,
    )

    await ctx.phase("dual-g20-mechanical-selection")
    winner, reason = _choose(candidate_a, candidate_b)
    candidates = {"A": candidate_a, "B": candidate_b}
    preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
    adopted, adoption_attempts = await _adopt(
        ctx,
        winner=winner,
        candidates=candidates,
        preserve_paths=preserve_paths,
    )
    selected = candidates.get(adopted) if adopted is not None else None
    return {
        "status": "done" if selected is not None else "incomplete",
        "winner": winner,
        "selection_reason": reason,
        "adopted": adopted,
        "adoption_attempts": adoption_attempts,
        "shared_public_command": shared_command,
        "candidates": {
            label: {
                "nonempty": bool(candidate.diff.strip()),
                "diff_bytes": len(candidate.diff.encode("utf-8")),
                "g20_result": _candidate_output(candidate).get("g20_result"),
                "public_command": _candidate_command(candidate),
                "public_test_records": _candidate_records(candidate),
            }
            for label, candidate in candidates.items()
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_wired_dual_g20_v1"]
