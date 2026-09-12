"""Exact G20 versus an autonomous coder with bounded contract adjudication."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import CandidateRun, workflow

from opencollab_eval.patch_diff import patch_paths

from . import validation_council_wired_dual_contract as contract
from . import validation_council_wired_dual_g20 as dual
from ._public_api import toolset
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)

AUTONOMOUS_CODER_BUDGET = 3_200_000
TOTAL_RUNNER_BUDGET = dual.CANDIDATE_G20_BUDGET + AUTONOMOUS_CODER_BUDGET + contract.CONTRACT_ADJUDICATOR_BUDGET

AUTONOMOUS_CODER_PROMPT = """\
You are the autonomous senior coder for one complete SWE repair attempt. Work
only in this isolated candidate worktree.

{rules}

Public issue
{goal}

Shared public command discovered by candidate A
{public_command}

Own the issue end to end. Locate the root cause, trace every affected producer,
consumer, and public API contract, and implement the smallest complete source
patch. Preserve compatible behavior outside the requested change. Inspect the
final diff and remove generated files, caches, logs, and accidental test edits.
Run the nearest relevant public tests with run_tests. When the shared public
command is present and representable by run_tests, execute the same target and
runner. Do not substitute an easier test. Finish with a non-empty source diff.
Do not use official results, hidden tests, FAIL_TO_PASS ids, grader patches, or
historical outcomes."""

CONTRACT_PROMPT = """\
You are the read-only contract adjudicator for candidate A from a complete G20
council and candidate B from an independent autonomous coder. You cannot edit,
merge, or rerun either candidate.

{rules}

Public issue
{goal}

Candidate evidence
{candidates}

Enumerate every explicit behavior requirement in the public issue. For each
requirement, compare the actual A and B diffs against the relevant producer,
consumer, and public API behavior. Cite concrete changed paths and diff details.
Public test records are comparable only when target, runner, and command are
identical. Do not reward larger diffs, stylistic changes, or unsupported claims.
Choose B only when public evidence shows B covers at least one requirement A
does not cover and no requirement is better covered by A. Set
requirements_complete true only after accounting for every explicit issue
requirement. Do not use official outcomes, hidden tests, FAIL_TO_PASS ids,
grader patches, historical results, or model identity."""


def _coder_tools() -> list[Any]:
    return toolset(
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "grep",
        "git_diff",
    )


async def _autonomous_candidate(
    ctx: Any,
    *,
    goal: str,
    shared_command: str,
) -> CandidateRun:
    tools = _coder_tools()
    try:
        raw = await ctx.candidate_agent(
            AUTONOMOUS_CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                public_command=shared_command or "(no shared public command)",
            ),
            label="g20-coder-contract-b",
            tools=tools,
            budget=AUTONOMOUS_CODER_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"autonomous coder unavailable after {type(exc).__name__}")
        return CandidateRun(
            label="g20-coder-contract-b",
            output=None,
            diff="",
            test_records=(),
            verified_targets=(),
        )
    records = [
        dual._public_record(record)
        for record in raw.test_records[: dual.MAX_PUBLIC_RECORDS]
        if isinstance(record, dict)
    ]
    observed_command = str(records[0].get("command") or "").strip() if records else shared_command
    return CandidateRun(
        label=raw.label,
        output={
            "coder_output": raw.output,
            "public_command": observed_command,
            "public_test_records": records,
        },
        diff=raw.diff,
        test_records=raw.test_records,
        verified_targets=raw.verified_targets,
    )


async def _contract_adjudicate(
    ctx: Any,
    *,
    goal: str,
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str, Any, str]:
    evidence, paths, truncated = contract._judge_input(candidate_a, candidate_b)
    if truncated:
        return "A", None, "contract-evidence-incomplete-default-a"
    try:
        result = await ctx.agent(
            CONTRACT_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                candidates=evidence,
            ),
            schema=contract.CONTRACT_SCHEMA,
            label="g20-coder-contract-adjudicator",
            tools=[],
            budget=contract.CONTRACT_ADJUDICATOR_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"G20/coder contract adjudicator unavailable after {type(exc).__name__}")
        result = None
    winner = contract._validated_judge_winner(result, paths)
    if winner is None:
        return "A", result, "contract-evidence-insufficient-default-a"
    return winner, result, "contract-adjudicated"


@workflow(
    name="validation-council-g20-coder-contract-v1",
    description="Exact G20 versus autonomous coder with public contract adjudication",
    phases=["g20-candidate", "autonomous-coder", "mechanical-selection", "contract-adjudication", "adoption"],
)
async def validation_council_g20_coder_contract_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Compare exact G20 with one differently structured autonomous coder."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("g20-coder-contract-a")
    candidate_a = await dual._candidate(
        ctx,
        label="g20-coder-contract-a",
        args=dict(args),
    )
    shared_command = dual._candidate_command(candidate_a)

    await ctx.phase("g20-coder-contract-b")
    candidate_b = await _autonomous_candidate(
        ctx,
        goal=goal,
        shared_command=shared_command,
    )

    await ctx.phase("g20-coder-contract-mechanical-selection")
    winner, reason, needs_judge = contract._mechanical_choice(
        candidate_a,
        candidate_b,
    )
    judge_result: Any = None
    if needs_judge:
        await ctx.phase("g20-coder-contract-adjudication")
        winner, judge_result, reason = await _contract_adjudicate(
            ctx,
            goal=goal,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
        )

    candidates = {"A": candidate_a, "B": candidate_b}
    preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
    adopted, adoption_attempts = await dual._adopt(
        ctx,
        winner=winner,
        candidates=candidates,
        preserve_paths=preserve_paths,
    )
    return {
        "status": "done" if adopted is not None else "incomplete",
        "winner": winner,
        "selection_reason": reason,
        "judge_used": needs_judge,
        "judge_result": judge_result,
        "adopted": adopted,
        "adoption_attempts": adoption_attempts,
        "shared_public_command": shared_command,
        "candidates": {
            "A": {
                "kind": "exact-g20",
                "nonempty": bool(candidate_a.diff.strip()),
                "diff_bytes": len(candidate_a.diff.encode("utf-8")),
                "changed_paths": patch_paths(candidate_a.diff)[:256],
                "g20_result": dual._candidate_output(candidate_a).get("g20_result"),
                "public_command": dual._candidate_command(candidate_a),
                "public_test_records": dual._candidate_records(candidate_a),
            },
            "B": {
                "kind": "autonomous-coder",
                "nonempty": bool(candidate_b.diff.strip()),
                "diff_bytes": len(candidate_b.diff.encode("utf-8")),
                "changed_paths": patch_paths(candidate_b.diff)[:256],
                "public_command": dual._candidate_command(candidate_b),
                "public_test_records": dual._candidate_records(candidate_b),
            },
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_g20_coder_contract_v1"]
