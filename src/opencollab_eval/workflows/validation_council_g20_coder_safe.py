"""Exact G20 versus autonomous coder with conservative mechanical choice."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import CandidateRun, workflow

from . import validation_council_g20_coder_contract as mixed
from . import validation_council_wired_dual_g20 as dual
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
)

CANDIDATE_BUDGET = 3_200_000
TOTAL_RUNNER_BUDGET = 2 * CANDIDATE_BUDGET
_HIDDEN_INPUT_KEYS = frozenset(
    {
        "fail_to_pass",
        "FAIL_TO_PASS",
        "pass_to_pass",
        "PASS_TO_PASS",
        "test_patch",
    }
)


def _blind_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in _HIDDEN_INPUT_KEYS}


async def _g20_candidate(
    ctx: Any,
    args: dict[str, Any],
) -> tuple[CandidateRun, bool]:
    try:
        candidate = await ctx.candidate_workflow(
            dual._exact_g20_candidate_workflow,
            _blind_args(args),
            label="g20-coder-safe-a",
            budget=CANDIDATE_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"G20 candidate unavailable after {type(exc).__name__}")
        return _empty_candidate("g20-coder-safe-a"), True
    return candidate, False


async def _coder_candidate(
    ctx: Any,
    *,
    goal: str,
    shared_command: str,
) -> tuple[CandidateRun, bool]:
    tools = mixed._coder_tools()
    try:
        raw = await ctx.candidate_agent(
            mixed.AUTONOMOUS_CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                public_command=shared_command or "(no shared public command)",
            ),
            label="g20-coder-safe-b",
            tools=tools,
            budget=CANDIDATE_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"autonomous coder unavailable after {type(exc).__name__}")
        return _empty_candidate("g20-coder-safe-b"), True
    records = [
        dual._public_record(record)
        for record in raw.test_records[: dual.MAX_PUBLIC_RECORDS]
        if isinstance(record, dict)
    ]
    command = str(records[0].get("command") or "").strip() if records else shared_command
    return (
        CandidateRun(
            label=raw.label,
            output={
                "coder_output": raw.output,
                "public_command": command,
                "public_test_records": records,
            },
            diff=raw.diff,
            test_records=raw.test_records,
            verified_targets=raw.verified_targets,
        ),
        False,
    )


def _empty_candidate(label: str) -> CandidateRun:
    return CandidateRun(
        label=label,
        output=None,
        diff="",
        test_records=(),
        verified_targets=(),
    )


def _verified_exit(record: dict[str, Any]) -> int | None:
    exit_code = record.get("exit_code")
    if (
        record.get("verified") is not True
        or not all(dual._record_key(record))
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
    ):
        return None
    return exit_code


def _b_has_exact_public_advantage(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> bool:
    records_a = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_a)
        if _verified_exit(record) is not None
    }
    records_b = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_b)
        if _verified_exit(record) is not None
    }
    return any(
        _verified_exit(records_b[key]) == 0 and (_verified_exit(records_a[key]) or 0) != 0
        for key in records_a.keys() & records_b.keys()
    )


def _choose(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
    *,
    candidate_failed: bool,
) -> tuple[str, str]:
    if candidate_failed:
        return "A", "candidate-exception-default-a"
    nonempty_a = bool(candidate_a.diff.strip())
    nonempty_b = bool(candidate_b.diff.strip())
    if not nonempty_a and nonempty_b:
        return "B", "only-b-nonempty"
    if not nonempty_b:
        return "A", "b-empty-default-a"
    if candidate_a.diff == candidate_b.diff:
        return "A", "identical-diff-default-a"
    if _b_has_exact_public_advantage(candidate_a, candidate_b):
        return "B", "same-command-b-green-a-red"
    return "A", "default-a"


async def _adopt(
    ctx: Any,
    *,
    winner: str,
    candidates: dict[str, CandidateRun],
    preserve_paths: list[str],
) -> tuple[str | None, list[str]]:
    order = ["B", "A"] if winner == "B" else ["A"]
    attempts = []
    for label in order:
        candidate = candidates[label]
        if not candidate.diff.strip():
            continue
        attempts.append(label)
        try:
            await ctx.adopt_candidate(candidate, preserve_paths=preserve_paths)
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"candidate adoption failed ({label}) after {type(exc).__name__}")
            continue
        return label, attempts
    return None, attempts


@workflow(
    name="validation-council-g20-coder-safe-v1",
    description="Exact G20 versus autonomous coder with safe mechanical choice",
    phases=["g20-candidate", "autonomous-coder", "mechanical-selection", "adoption"],
)
async def validation_council_g20_coder_safe_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Select B only for nonempty or exact public-test evidence."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    await ctx.phase("g20-coder-safe-a")
    candidate_a, failed_a = await _g20_candidate(ctx, args)
    await ctx.phase("g20-coder-safe-b")
    candidate_b, failed_b = await _coder_candidate(
        ctx,
        goal=goal,
        shared_command=dual._candidate_command(candidate_a),
    )
    winner, reason = _choose(
        candidate_a,
        candidate_b,
        candidate_failed=failed_a or failed_b,
    )
    candidates = {"A": candidate_a, "B": candidate_b}
    preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
    adopted, adoption_attempts = await _adopt(
        ctx,
        winner=winner,
        candidates=candidates,
        preserve_paths=preserve_paths,
    )
    return {
        "status": "done" if adopted is not None else "incomplete",
        "winner": winner,
        "selection_reason": reason,
        "adopted": adopted,
        "adoption_attempts": adoption_attempts,
        "candidates": {
            label: {
                "nonempty": bool(candidate.diff.strip()),
                "diff_bytes": len(candidate.diff.encode("utf-8")),
                "public_test_records": dual._candidate_records(candidate),
            }
            for label, candidate in candidates.items()
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_g20_coder_safe_v1"]
