"""Dual exact G20 with mechanical priority and bounded contract adjudication."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from opencollab.workflows import CandidateRun, workflow

from opencollab_eval.patch_diff import patch_paths

from . import validation_council_wired_dual_g20 as dual
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    structured_role_timeout_seconds,
)

CONTRACT_ADJUDICATOR_BUDGET = 500_000
TOTAL_RUNNER_BUDGET = dual.TOTAL_RUNNER_BUDGET + CONTRACT_ADJUDICATOR_BUDGET
MAX_DIFF_BYTES = 240_000
MAX_JUDGE_INPUT_BYTES = 600_000
MAX_REQUIREMENTS = 64
COVERAGE_VALUES = frozenset({"covered", "not_covered", "unclear"})

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": ["A", "B"]},
        "requirements_complete": {"type": "boolean"},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_REQUIREMENTS,
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "a_coverage": {
                        "type": "string",
                        "enum": sorted(COVERAGE_VALUES),
                    },
                    "b_coverage": {
                        "type": "string",
                        "enum": sorted(COVERAGE_VALUES),
                    },
                    "a_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "b_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "requirement",
                    "a_coverage",
                    "b_coverage",
                    "a_evidence",
                    "b_evidence",
                ],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["winner", "requirements_complete", "requirements", "rationale"],
    "additionalProperties": False,
}

CONTRACT_PROMPT = """\
You are the read-only contract adjudicator for two complete independent G20
candidates. You cannot edit or merge either candidate.

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
Choose B only when the supplied public evidence shows that B covers at least
one requirement A does not cover and no requirement is better covered by A.
Set requirements_complete true only after accounting for every explicit issue
requirement. Do not use official outcomes, hidden tests, FAIL_TO_PASS ids,
grader patches, historical results, or model identity."""


def _clip_text(value: Any, limit: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text, False
    return payload[:limit].decode("utf-8", errors="ignore"), True


def _mechanical_choice(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str | None, str, bool]:
    nonempty_a = bool(candidate_a.diff.strip())
    nonempty_b = bool(candidate_b.diff.strip())
    if nonempty_a and not nonempty_b:
        return "A", "only-a-nonempty", False
    if nonempty_b and not nonempty_a:
        return "B", "only-b-nonempty", False
    if not nonempty_a and not nonempty_b:
        return None, "both-empty", False
    if candidate_a.diff == candidate_b.diff:
        return "A", "identical-diff", False
    public_winner = dual._public_red_winner(candidate_a, candidate_b)
    if public_winner is not None:
        return public_winner, "same-command-public-red", False
    return None, "contract-adjudication", True


def _shared_public_records(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> list[dict[str, Any]]:
    records_a = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_a)
        if all(dual._record_key(record))
    }
    records_b = {
        dual._record_key(record): record
        for record in dual._candidate_records(candidate_b)
        if all(dual._record_key(record))
    }
    return [
        {
            "target": key[0],
            "runner": key[1],
            "command": key[2],
            "A": {
                "exit_code": records_a[key].get("exit_code"),
                "verified": records_a[key].get("verified") is True,
            },
            "B": {
                "exit_code": records_b[key].get("exit_code"),
                "verified": records_b[key].get("verified") is True,
            },
        }
        for key in sorted(records_a.keys() & records_b.keys())
    ]


def _candidate_view(candidate: CandidateRun) -> tuple[dict[str, Any], bool]:
    diff, truncated = _clip_text(candidate.diff, MAX_DIFF_BYTES)
    return {
        "diff": diff,
        "diff_truncated": truncated,
        "changed_paths": patch_paths(candidate.diff)[:256],
        "public_command": dual._candidate_command(candidate),
    }, truncated


def _judge_input(
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str, dict[str, list[str]], bool]:
    view_a, truncated_a = _candidate_view(candidate_a)
    view_b, truncated_b = _candidate_view(candidate_b)
    payload = {
        "A": view_a,
        "B": view_b,
        "shared_public_test_records": _shared_public_records(
            candidate_a,
            candidate_b,
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    text, payload_truncated = _clip_text(encoded, MAX_JUDGE_INPUT_BYTES)
    paths = {
        "A": list(view_a["changed_paths"]),
        "B": list(view_b["changed_paths"]),
    }
    return text, paths, truncated_a or truncated_b or payload_truncated


def _evidence_mentions_changed_path(
    evidence: list[Any],
    paths: list[str],
) -> bool:
    anchors = [str(item).replace("\\", "/") for item in evidence if str(item)]
    for path in paths:
        normalized = str(path).replace("\\", "/")
        name = PurePosixPath(normalized).name
        if any(normalized in anchor or name and name in anchor for anchor in anchors):
            return True
    return False


def _validated_judge_winner(
    result: Any,
    paths: dict[str, list[str]],
) -> str | None:
    if not isinstance(result, dict) or result.get("requirements_complete") is not True:
        return None
    winner = result.get("winner")
    requirements = result.get("requirements")
    if winner not in {"A", "B"} or not isinstance(requirements, list):
        return None
    if not 1 <= len(requirements) <= MAX_REQUIREMENTS:
        return None
    loser = "B" if winner == "A" else "A"
    advantage = False
    for item in requirements:
        if not isinstance(item, dict) or not str(item.get("requirement") or "").strip():
            return None
        coverage_a = item.get("a_coverage")
        coverage_b = item.get("b_coverage")
        evidence_a = item.get("a_evidence")
        evidence_b = item.get("b_evidence")
        if (
            coverage_a not in COVERAGE_VALUES
            or coverage_b not in COVERAGE_VALUES
            or not isinstance(evidence_a, list)
            or not isinstance(evidence_b, list)
        ):
            return None
        coverage = {"A": coverage_a, "B": coverage_b}
        evidence = {"A": evidence_a, "B": evidence_b}
        if coverage[loser] == "covered" and coverage[winner] == "not_covered":
            return None
        if coverage[winner] == "covered" and coverage[loser] == "not_covered":
            if not _evidence_mentions_changed_path(evidence[winner], paths[winner]):
                return None
            advantage = True
    return winner if advantage else None


async def _contract_adjudicate(
    ctx: Any,
    *,
    goal: str,
    candidate_a: CandidateRun,
    candidate_b: CandidateRun,
) -> tuple[str, Any, str]:
    evidence, paths, truncated = _judge_input(candidate_a, candidate_b)
    if truncated:
        return "A", None, "contract-evidence-incomplete-default-a"
    try:
        result = await ctx.agent(
            CONTRACT_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                candidates=evidence,
            ),
            schema=CONTRACT_SCHEMA,
            label="dual-g20-contract-adjudicator",
            tools=[],
            budget=CONTRACT_ADJUDICATOR_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"dual G20 contract adjudicator unavailable after {type(exc).__name__}")
        result = None
    winner = _validated_judge_winner(result, paths)
    if winner is None:
        return "A", result, "contract-evidence-insufficient-default-a"
    return winner, result, "contract-adjudicated"


@workflow(
    name="validation-council-wired-dual-contract-v1",
    description="Dual exact G20 with bounded public contract adjudication",
    phases=["candidate-a", "candidate-b", "mechanical-selection", "contract-adjudication", "adoption"],
)
async def validation_council_wired_dual_contract_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Apply mechanical evidence first and contract adjudication only if needed."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("dual-contract-candidate-a")
    candidate_a = await dual._candidate(
        ctx,
        label="dual-contract-a",
        args=dict(args),
    )
    shared_command = dual._candidate_command(candidate_a)

    await ctx.phase("dual-contract-candidate-b")
    candidate_b_args = dict(args)
    candidate_b_args["dual_g20_public_command"] = shared_command
    candidate_b = await dual._candidate(
        ctx,
        label="dual-contract-b",
        args=candidate_b_args,
    )

    await ctx.phase("dual-contract-mechanical-selection")
    winner, reason, needs_judge = _mechanical_choice(candidate_a, candidate_b)
    judge_result: Any = None
    if needs_judge:
        await ctx.phase("dual-contract-adjudication")
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
            label: {
                "nonempty": bool(candidate.diff.strip()),
                "diff_bytes": len(candidate.diff.encode("utf-8")),
                "changed_paths": patch_paths(candidate.diff)[:256],
                "g20_result": dual._candidate_output(candidate).get("g20_result"),
                "public_command": dual._candidate_command(candidate),
                "public_test_records": dual._candidate_records(candidate),
            }
            for label, candidate in candidates.items()
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_wired_dual_contract_v1"]
