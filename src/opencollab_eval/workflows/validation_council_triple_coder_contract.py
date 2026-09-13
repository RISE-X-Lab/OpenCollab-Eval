"""Three isolated coder candidates with public evidence selection."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from opencollab.workflows import CandidateRun, workflow

from opencollab_eval.patch_diff import patch_paths

from . import validation_council_wired_dual_g20 as dual
from ._public_api import toolset
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)

CANDIDATE_BUDGET = None
CONTRACT_JUDGE_BUDGET = None
TOTAL_RUNNER_BUDGET = None
MAX_DIFF_BYTES = 240_000
MAX_JUDGE_INPUT_BYTES = 800_000
MAX_REQUIREMENTS = 64
COVERAGE_VALUES = frozenset({"covered", "not_covered", "unclear"})
LABELS = ("A", "B", "C")

MINIMAL_PROMPT = """\
You are autonomous coder A in an isolated candidate worktree.

{rules}

Public issue
{goal}

Find the narrowest root cause that fully explains the issue. Trace the immediate
callers and consumers needed for correctness, implement a minimal complete
source patch, run the nearest relevant public tests with run_tests, inspect the
final diff, and finish with a non-empty source diff. Preserve unrelated public
behavior. Avoid tests, generated files, caches, and logs. Do not use official
results, hidden tests, FAIL_TO_PASS ids, grader patches, or historical results."""

CROSS_COMPONENT_PROMPT = """\
You are autonomous coder B in an isolated candidate worktree.

{rules}

Public issue
{goal}

Shared public command observed from candidate A
{public_command}

Solve the issue end to end. Trace producers, consumers, public API and
serialization boundaries, lifecycle behavior, error propagation, and edge
cases. Implement the smallest complete cross-component source patch. Run
relevant public tests with run_tests. When the shared command is representable,
run the same target and runner. Inspect the final diff and finish with a
non-empty source diff. Avoid tests, generated files, caches, and logs. Do not
use official results, hidden tests, FAIL_TO_PASS ids, grader patches, or
historical results."""

TEST_FIRST_PROMPT = """\
You are autonomous coder C in an isolated candidate worktree.

{rules}

Public issue
{goal}

Shared public command observed from candidate A
{public_command}

Work as a test-first debugger. Reproduce the public failure or nearest public
behavior before editing. Trace the failing value and control flow to its root
cause, then implement the smallest source repair that makes the same public
target green without weakening coverage. Run the exact shared target and runner
when representable, plus a focused regression probe for the changed contract.
Inspect the final diff and finish with a non-empty source diff. Avoid tests,
generated files, caches, and logs. Do not use official results, hidden tests,
FAIL_TO_PASS ids, grader patches, or historical results."""

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": list(LABELS)},
        "requirements_complete": {"type": "boolean"},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_REQUIREMENTS,
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    **{
                        f"{label.lower()}_coverage": {
                            "type": "string",
                            "enum": sorted(COVERAGE_VALUES),
                        }
                        for label in LABELS
                    },
                    **{
                        f"{label.lower()}_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                        for label in LABELS
                    },
                },
                "required": [
                    "requirement",
                    *(f"{label.lower()}_coverage" for label in LABELS),
                    *(f"{label.lower()}_evidence" for label in LABELS),
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
You are the read-only contract adjudicator for three isolated coder candidates.
You cannot edit, merge, rerun, or combine candidates.

{rules}

Public issue
{goal}

Candidate evidence
{candidates}

Enumerate every explicit behavior requirement in the public issue. Compare the
actual A, B, and C diffs against each requirement and cite changed paths and
concrete diff details. Public tests are comparable only for identical target,
runner, and command. Choose one complete candidate. Do not reward patch size or
style. Set requirements_complete true only after accounting for every explicit
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


async def _candidate(
    ctx: Any,
    *,
    label: str,
    prompt: str,
    goal: str,
    shared_command: str = "",
) -> CandidateRun:
    raw = await ctx.candidate_agent(
        prompt.format(
            rules=SHARED_RULES,
            goal=goal,
            public_command=shared_command or "(no shared public command)",
        ),
        label=label,
        tools=_coder_tools(),
        budget=CANDIDATE_BUDGET,
        timeout=coder_role_timeout_seconds(),
    )
    records = [
        dual._public_record(record)
        for record in raw.test_records[: dual.MAX_PUBLIC_RECORDS]
        if isinstance(record, dict)
    ]
    command = str(records[0].get("command") or "").strip() if records else shared_command
    return CandidateRun(
        label=raw.label,
        output={
            "coder_output": raw.output,
            "public_command": command,
            "public_test_records": records,
        },
        diff=raw.diff,
        test_records=raw.test_records,
        verified_targets=raw.verified_targets,
    )


def _candidate_states(candidate: CandidateRun) -> dict[tuple[str, str, str], str]:
    return {
        dual._record_key(record): state
        for record in dual._candidate_records(candidate)
        if (state := dual._record_state(record)) is not None
    }


def _public_unique_winner(candidates: dict[str, CandidateRun]) -> str | None:
    active = {label: candidate for label, candidate in candidates.items() if candidate.diff.strip()}
    if len(active) < 2:
        return None
    states = {label: _candidate_states(candidate) for label, candidate in active.items()}
    shared_keys = set.intersection(*(set(records) for records in states.values()))
    decisions: set[str] = set()
    for key in shared_keys:
        values = {label: states[label][key] for label in active}
        green = [label for label, state in values.items() if state == "green"]
        if len(green) == 1 and all(state == "red" for label, state in values.items() if label != green[0]):
            decisions.add(green[0])
    return next(iter(decisions)) if len(decisions) == 1 else None


def _mechanical_choice(
    candidates: dict[str, CandidateRun],
) -> tuple[str | None, str, bool]:
    nonempty = [label for label in LABELS if candidates[label].diff.strip()]
    if not nonempty:
        return None, "all-empty", False
    if len(nonempty) == 1:
        return nonempty[0], "only-nonempty", False
    diffs = {candidates[label].diff for label in nonempty}
    if len(diffs) == 1:
        return nonempty[0], "identical-nonempty-diff", False
    public_winner = _public_unique_winner(candidates)
    if public_winner is not None:
        return public_winner, "same-command-public-unique-green", False
    return None, "contract-adjudication", True


def _clip(value: Any, limit: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    payload = text.encode()
    if len(payload) <= limit:
        return text, False
    return payload[:limit].decode(errors="ignore"), True


def _candidate_view(candidate: CandidateRun) -> tuple[dict[str, Any], bool]:
    diff, truncated = _clip(candidate.diff, MAX_DIFF_BYTES)
    return {
        "diff": diff,
        "diff_truncated": truncated,
        "changed_paths": patch_paths(candidate.diff)[:256],
        "public_command": dual._candidate_command(candidate),
    }, truncated


def _shared_records(candidates: dict[str, CandidateRun]) -> list[dict[str, Any]]:
    records = {
        label: {
            dual._record_key(record): record
            for record in dual._candidate_records(candidate)
            if all(dual._record_key(record))
        }
        for label, candidate in candidates.items()
    }
    keys = set.intersection(*(set(value) for value in records.values()))
    return [
        {
            "target": key[0],
            "runner": key[1],
            "command": key[2],
            **{
                label: {
                    "exit_code": records[label][key].get("exit_code"),
                    "verified": records[label][key].get("verified") is True,
                }
                for label in LABELS
            },
        }
        for key in sorted(keys)
    ]


def _judge_input(
    candidates: dict[str, CandidateRun],
) -> tuple[str, dict[str, list[str]], bool]:
    views: dict[str, dict[str, Any]] = {}
    truncated = False
    for label in LABELS:
        view, clipped = _candidate_view(candidates[label])
        views[label] = view
        truncated = truncated or clipped
    payload = {**views, "shared_public_test_records": _shared_records(candidates)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text, payload_clipped = _clip(encoded, MAX_JUDGE_INPUT_BYTES)
    paths = {label: list(views[label]["changed_paths"]) for label in LABELS}
    return text, paths, truncated or payload_clipped


def _mentions_path(evidence: list[Any], paths: list[str]) -> bool:
    anchors = [str(item).replace("\\", "/") for item in evidence if str(item)]
    for path in paths:
        normalized = str(path).replace("\\", "/")
        name = PurePosixPath(normalized).name
        if any(normalized in anchor or (name and name in anchor) for anchor in anchors):
            return True
    return False


def _validated_winner(result: Any, paths: dict[str, list[str]]) -> str | None:
    if not isinstance(result, dict) or result.get("requirements_complete") is not True:
        return None
    winner = result.get("winner")
    requirements = result.get("requirements")
    if winner not in LABELS or not isinstance(requirements, list):
        return None
    if not 1 <= len(requirements) <= MAX_REQUIREMENTS or not paths[winner]:
        return None
    advantage = False
    for item in requirements:
        if not isinstance(item, dict) or not str(item.get("requirement") or "").strip():
            return None
        coverage = {label: item.get(f"{label.lower()}_coverage") for label in LABELS}
        evidence = {label: item.get(f"{label.lower()}_evidence") for label in LABELS}
        if any(value not in COVERAGE_VALUES for value in coverage.values()) or any(
            not isinstance(value, list) for value in evidence.values()
        ):
            return None
        for loser in LABELS:
            if loser == winner:
                continue
            if coverage[loser] == "covered" and coverage[winner] == "not_covered":
                return None
            if coverage[winner] == "covered" and coverage[loser] == "not_covered":
                advantage = advantage or _mentions_path(evidence[winner], paths[winner])
    return winner if advantage else None


async def _adjudicate(
    ctx: Any,
    *,
    goal: str,
    candidates: dict[str, CandidateRun],
) -> tuple[str, Any, str]:
    evidence, paths, truncated = _judge_input(candidates)
    if truncated:
        return "A", None, "contract-evidence-incomplete-default-a"
    try:
        result = await ctx.agent(
            CONTRACT_PROMPT.format(rules=SHARED_RULES, goal=goal, candidates=evidence),
            schema=CONTRACT_SCHEMA,
            label="triple-coder-contract-adjudicator",
            tools=[],
            budget=CONTRACT_JUDGE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"triple coder adjudicator unavailable after {type(exc).__name__}")
        result = None
    winner = _validated_winner(result, paths)
    if winner is None:
        return "A", result, "contract-evidence-insufficient-default-a"
    return winner, result, "contract-adjudicated"


async def _adopt(
    ctx: Any,
    *,
    winner: str | None,
    candidates: dict[str, CandidateRun],
    preserve_paths: list[str],
) -> tuple[str | None, list[str]]:
    if winner is None or not candidates[winner].diff.strip():
        return None, []
    try:
        await ctx.adopt_candidate(candidates[winner], preserve_paths=preserve_paths)
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"triple coder adoption failed after {type(exc).__name__}")
        return None, [winner]
    return winner, [winner]


@workflow(
    name="validation-council-triple-coder-contract-v1",
    description="Three isolated coder strategies with public contract choice",
    phases=[
        "minimal-coder",
        "cross-component-coder",
        "test-first-coder",
        "mechanical-selection",
        "contract-adjudication",
        "adoption",
    ],
)
async def validation_council_triple_coder_contract_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run three isolated coders and transactionally adopt one winner."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    source_before = await ctx.diff()
    await ctx.phase("triple-coder-a")
    candidate_a = await _candidate(ctx, label="triple-coder-a", prompt=MINIMAL_PROMPT, goal=goal)
    shared_command = dual._candidate_command(candidate_a)
    await ctx.phase("triple-coder-b")
    candidate_b = await _candidate(
        ctx,
        label="triple-coder-b",
        prompt=CROSS_COMPONENT_PROMPT,
        goal=goal,
        shared_command=shared_command,
    )
    await ctx.phase("triple-coder-c")
    candidate_c = await _candidate(
        ctx,
        label="triple-coder-c",
        prompt=TEST_FIRST_PROMPT,
        goal=goal,
        shared_command=shared_command,
    )
    candidates = {"A": candidate_a, "B": candidate_b, "C": candidate_c}
    await ctx.phase("triple-coder-mechanical-selection")
    winner, reason, judge_used = _mechanical_choice(candidates)
    judge_result: Any = None
    if judge_used:
        await ctx.phase("triple-coder-contract-adjudication")
        winner, judge_result, reason = await _adjudicate(
            ctx,
            goal=goal,
            candidates=candidates,
        )
    if await ctx.diff() != source_before:
        raise RuntimeError("candidate_workspace_tracking_failure")
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
        "judge_used": judge_used,
        "judge_result": judge_result,
        "adopted": adopted,
        "adoption_attempts": adoption_attempts,
        "shared_public_command": shared_command,
        "candidates": {
            label: {
                "kind": kind,
                "nonempty": bool(candidate.diff.strip()),
                "diff_bytes": len(candidate.diff.encode()),
                "changed_paths": patch_paths(candidate.diff)[:256],
                "public_command": dual._candidate_command(candidate),
                "public_test_records": dual._candidate_records(candidate),
            }
            for label, candidate, kind in (
                ("A", candidate_a, "minimal-root-cause-coder"),
                ("B", candidate_b, "cross-component-contract-coder"),
                ("C", candidate_c, "test-first-debugger"),
            )
        },
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_triple_coder_contract_v1"]
