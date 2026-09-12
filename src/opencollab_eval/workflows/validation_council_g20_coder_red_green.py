"""Exact G20 anchored to a minimal coder by comparable public test evidence."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import CandidateRun, workflow

from . import validation_council_dual_coder_contract as coders
from . import validation_council_wired_dual_g20 as dual
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)

CANDIDATE_BUDGET = 3_200_000
MINIMAL_CODER_BUDGET = 3_000_000
TOTAL_RUNNER_BUDGET = 2 * CANDIDATE_BUDGET
_SHARED_COMMAND_KEY = "g20_coder_red_green_public_command"
_SHARED_PROBE_KEY = "g20_coder_red_green_public_probe"
_HIDDEN_INPUT_KEYS = frozenset(
    {
        "fail_to_pass",
        "FAIL_TO_PASS",
        "pass_to_pass",
        "PASS_TO_PASS",
        "test_patch",
    }
)

MINIMAL_CODER_PROMPT = """\
You are the minimal root-cause coder. Work only in this isolated candidate
worktree.

{rules}

Public issue
{goal}

Shared public command selected by the exact G20 candidate
{public_command}

Find the narrowest root cause that fully explains the issue. Preserve backward
compatibility and existing public behavior outside the requested change. Trace
the immediate callers and consumers needed to verify the fix, then implement a
minimal complete source patch. Avoid broad refactors, speculative cleanup, test
edits, generated files, caches, and logs. Run the nearest relevant public tests
with run_tests. When the shared public command is present and representable,
execute the same target and runner without replacing it with an easier test.
Inspect the final diff and finish with a non-empty source diff. Do not use
official results, hidden tests, FAIL_TO_PASS ids, grader patches, or historical
outcomes."""

REFERENCE_PROBE_PROMPT = """\
You are the read-only comparable public test executor after the minimal coder.
You cannot modify the candidate worktree.

{rules}

Public issue
{goal}

Exact public probe already executed by candidate A
Target: {target}
Runner: {runner}
Command: {command}

Execute the same target with the same runner through run_tests. Do not replace
it with a broader, narrower, easier, or different test. When the exact probe
cannot be represented, do not run a substitute. Report only commands actually
executed. Do not use official results, hidden tests, FAIL_TO_PASS identifiers,
grader patches, or historical outcomes."""


def _blind_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in _HIDDEN_INPUT_KEYS}


def _empty_candidate(label: str) -> CandidateRun:
    return CandidateRun(
        label=label,
        output=None,
        diff="",
        test_records=(),
        verified_targets=(),
    )


async def _g20_candidate(ctx: Any, args: dict[str, Any]) -> CandidateRun:
    try:
        return await ctx.candidate_workflow(
            dual._exact_g20_candidate_workflow,
            _blind_args(args),
            label="g20-coder-red-green-a",
            budget=CANDIDATE_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"G20 candidate unavailable after {type(exc).__name__}")
        return _empty_candidate("g20-coder-red-green-a")


def _reference_probe(candidate: CandidateRun) -> dict[str, Any]:
    for record in dual._candidate_records(candidate):
        key = dual._record_key(record)
        exit_code = record.get("exit_code")
        if (
            all(key)
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and isinstance(record.get("verified"), bool)
        ):
            return {
                "target": key[0],
                "runner": key[1],
                "command": key[2],
            }
    return {}


async def _run_reference_probe(
    ctx: Any,
    *,
    goal: str,
    reference: dict[str, Any],
) -> tuple[Any, list[dict[str, Any]]]:
    target = str(reference.get("target") or "").strip()
    runner = str(reference.get("runner") or "").strip()
    command = str(reference.get("command") or "").strip()
    if not target or not runner or not command:
        return None, []
    tools = dual._probe_tools()
    try:
        output = await ctx.agent(
            REFERENCE_PROBE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                target=target,
                runner=runner,
                command=command,
            ),
            schema=dual.PUBLIC_PROBE_SCHEMA,
            label="g20-coder-red-green-b-public-probe",
            tools=tools,
            budget=dual.PUBLIC_PROBE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"minimal coder public probe unavailable after {type(exc).__name__}")
        output = None
    return output, dual._verification_records(tools)


async def _minimal_candidate_workflow(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    shared_command = str(args.get(_SHARED_COMMAND_KEY) or "").strip()
    shared_probe = args.get(_SHARED_PROBE_KEY)
    reference = dict(shared_probe) if isinstance(shared_probe, dict) else {}
    tools = coders._coder_tools()
    coder_output: Any = None
    coder_failure: str | None = None
    try:
        coder_output = await ctx.agent(
            MINIMAL_CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                public_command=shared_command or "(no shared public command)",
            ),
            label="g20-coder-red-green-b-coder",
            tools=tools,
            budget=MINIMAL_CODER_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        coder_failure = type(exc).__name__
        await ctx.log(f"minimal coder unavailable after {coder_failure}")

    probe, records = await _run_reference_probe(
        ctx,
        goal=goal,
        reference=reference,
    )
    return {
        "coder_output": coder_output,
        "coder_failure": coder_failure,
        "public_command": shared_command,
        "public_probe": probe,
        "public_test_records": records,
    }


async def _minimal_candidate(
    ctx: Any,
    args: dict[str, Any],
    *,
    shared_command: str,
    shared_probe: dict[str, Any],
) -> CandidateRun:
    candidate_args = _blind_args(args)
    candidate_args[_SHARED_COMMAND_KEY] = shared_command
    candidate_args[_SHARED_PROBE_KEY] = dict(shared_probe)
    try:
        return await ctx.candidate_workflow(
            _minimal_candidate_workflow,
            candidate_args,
            label="g20-coder-red-green-b",
            budget=CANDIDATE_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"minimal coder candidate unavailable after {type(exc).__name__}")
        return _empty_candidate("g20-coder-red-green-b")


def _choose(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str | None, str]:
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
    public_winner = dual._public_red_winner(candidate_a, candidate_b)
    if public_winner == "B":
        return "B", "same-command-a-red-b-green"
    if public_winner == "A":
        return "A", "same-command-a-green-b-red"
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
    order = ["B", "A"] if winner == "B" else ["A"]
    attempts: list[str] = []
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
    name="validation-council-g20-coder-red-green-v1",
    description="Exact G20 with a minimal coder selected only by shared public red-green evidence",
    phases=[
        "g20-candidate",
        "minimal-coder",
        "shared-public-probe",
        "mechanical-selection",
        "adoption",
    ],
)
async def validation_council_g20_coder_red_green_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Keep exact G20 unless one comparable public test proves B better."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("g20-coder-red-green-a")
    candidate_a = await _g20_candidate(ctx, args)
    shared_command = dual._candidate_command(candidate_a)
    shared_probe = _reference_probe(candidate_a)

    await ctx.phase("g20-coder-red-green-b")
    candidate_b = await _minimal_candidate(
        ctx,
        args,
        shared_command=shared_command,
        shared_probe=shared_probe,
    )

    await ctx.phase("g20-coder-red-green-selection")
    winner, reason = _choose(candidate_a, candidate_b)
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
        "shared_public_command": shared_command,
        "candidates": {
            label: {
                "kind": "exact-g20" if label == "A" else "minimal-root-cause-coder",
                "nonempty": bool(candidate.diff.strip()),
                "diff_bytes": len(candidate.diff.encode("utf-8")),
                "public_command": dual._candidate_command(candidate),
                "public_test_records": dual._candidate_records(candidate),
            }
            for label, candidate in candidates.items()
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_g20_coder_red_green_v1"]
