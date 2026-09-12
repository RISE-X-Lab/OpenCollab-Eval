"""Evidence-carrying repair council for SWE-style repository tasks."""

from __future__ import annotations

import json
from typing import Any

from opencollab.workflows import workflow

from ._public_api import toolset
from ._validation_council_solve_defs import (
    _coder_tools,
    _complete_goal,
    _dict_or,
    _read_tools,
    _source_diff_present,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)

SCOUT_BUDGET = 120_000
LEAD_CODER_BUDGET = 500_000
CRITIC_BUDGET = 90_000
BACKUP_CODER_BUDGET = 260_000
MAX_HANDOFF_BYTES = 24_000

RULES = """\
Use only the public issue, repository, public tests, and public documentation.
Never inspect hidden grader data, grader patches, or FAIL_TO_PASS identifiers.
Every factual claim must cite a repository path, symbol, command, or observed output.
Run real commands when the role has execution tools. Keep temporary probes under /tmp.
Do not commit. Preserve a non-empty source patch even when internal review is uncertain."""

ROOT_CAUSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypotheses", "primary", "files", "evidence", "risks"],
    "properties": {
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "primary": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["anchor", "observation"],
                "properties": {
                    "anchor": {"type": "string"},
                    "observation": {"type": "string"},
                },
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

REPRODUCTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "framework",
        "planned_commands",
        "observations",
        "recommended_gate",
    ],
    "properties": {
        "framework": {"type": "string"},
        "planned_commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "rationale", "expected_signal"],
                "properties": {
                    "command": {"type": "string"},
                    "rationale": {"type": "string"},
                    "expected_signal": {"type": "string"},
                },
            },
        },
        "observations": {"type": "array", "items": {"type": "string"}},
        "recommended_gate": {"type": "string"},
    },
}

CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings", "evidence", "repair_brief"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
        "findings": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["anchor", "observation"],
                "properties": {
                    "anchor": {"type": "string"},
                    "observation": {"type": "string"},
                },
            },
        },
        "repair_brief": {"type": "string"},
    },
}

ROOT_CAUSE_PROMPT = """\
You are the root-cause scout on a software repair council.

{rules}

Issue
{goal}

Trace the failing behavior through concrete symbols and call paths. Rank at least two
plausible hypotheses when evidence permits. Identify the smallest coherent edit scope.
Return structured evidence with paths and symbols. Do not edit files."""

REPRODUCTION_PROMPT = """\
You are the reproduction scout on a software repair council.

{rules}

Issue
{goal}

Find the repository's real test and build entry points from source and configuration.
Recommend bounded public baseline and post-patch commands, but do not execute commands
or edit files. Record exact paths, test targets, fixtures, and expected signals."""

CODER_PROMPT = """\
You are the lead implementer. Own the repair end to end.

{rules}

Issue
{goal}

Root-cause scout report
{root_cause}

Reproduction scout report
{reproduction}

Inspect any missing context yourself. Reproduce when feasible, implement the smallest
complete source fix, and run targeted tests plus a relevant regression or build check.
You may revise your own patch after observing failures. Leave the best source diff in
the worktree. Finish with a concise report of edits and commands actually run."""

BACKUP_CODER_PROMPT = """\
You are the backup implementer. The first implementer left no source diff.

{rules}

Issue
{goal}

Evidence package
{evidence}

Develop an alternative root-cause hypothesis, inspect the repository, implement a
non-empty source fix, and run a targeted command. Leave the best patch in the worktree."""

CORRECTNESS_PROMPT = """\
You are the behavioral correctness critic.

{rules}

Issue
{goal}

Evidence package
{evidence}

Read the actual git diff and affected sources. Check the patch against the issue,
root-cause evidence, and repository tests. Cite exact paths, symbols, and diff evidence.
Give precise repair instructions on failure. Do not execute commands or edit files."""

COMPATIBILITY_PROMPT = """\
You are the compatibility and integration critic.

{rules}

Issue
{goal}

Evidence package
{evidence}

Read the actual git diff and affected call sites. Check API compatibility, cross-file
integration, generated assets, configuration, and regression scope. Cite exact paths,
symbols, and diff evidence. Give precise repair instructions. Do not execute commands
or edit files."""


def _bounded_json(value: Any, limit: int = MAX_HANDOFF_BYTES) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = raw.encode("utf-8")
    if len(payload) <= limit:
        return raw
    marker = "...[handoff clipped]..."
    retained = limit - len(marker.encode("utf-8"))
    head = payload[: retained * 3 // 4].decode("utf-8", errors="ignore")
    tail = payload[-(retained // 4) :].decode("utf-8", errors="ignore")
    return head + marker + tail


def _empty_root_cause() -> dict[str, Any]:
    return {
        "hypotheses": [],
        "primary": "Scout unavailable; lead implementer must localize directly.",
        "files": [],
        "evidence": [],
        "risks": [],
    }


def _empty_reproduction() -> dict[str, Any]:
    return {
        "framework": "",
        "planned_commands": [],
        "observations": ["Reproduction scout unavailable."],
        "recommended_gate": "Lead implementer must discover a bounded public gate.",
    }


def _empty_critic(label: str) -> dict[str, Any]:
    return {
        "verdict": "BLOCKED",
        "findings": [f"{label} returned no structured evidence."],
        "evidence": [],
        "repair_brief": f"Re-run {label} checks during repair.",
    }


def _critic_tools() -> list[Any]:
    return toolset("file_read", "grep", "git_diff")


async def _critics(
    ctx: Any,
    *,
    goal: str,
    evidence: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reports = await ctx.parallel(
        [
            lambda: ctx.agent(
                CORRECTNESS_PROMPT.format(
                    rules=RULES,
                    goal=goal,
                    evidence=evidence,
                ),
                schema=CRITIC_SCHEMA,
                label="correctness-critic",
                tools=_critic_tools(),
                budget=CRITIC_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
            lambda: ctx.agent(
                COMPATIBILITY_PROMPT.format(
                    rules=RULES,
                    goal=goal,
                    evidence=evidence,
                ),
                schema=CRITIC_SCHEMA,
                label="compatibility-critic",
                tools=_critic_tools(),
                budget=CRITIC_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
        ]
    )
    correctness = _dict_or(
        reports[0] if reports else None,
        _empty_critic("correctness critic"),
    )
    compatibility = _dict_or(
        reports[1] if len(reports) > 1 else None,
        _empty_critic("compatibility critic"),
    )
    return correctness, compatibility


@workflow(
    name="evidence-action-council-v1",
    description="Lean evidence-carrying repair council with read-only diff critics",
    phases=["scout", "implement", "critic"],
)
async def evidence_action_council_v1(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    injected_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]

    await ctx.phase("scout")
    scouts = await ctx.parallel(
        [
            lambda: ctx.agent(
                ROOT_CAUSE_PROMPT.format(rules=RULES, goal=goal),
                schema=ROOT_CAUSE_SCHEMA,
                label="root-cause-scout",
                tools=_read_tools(),
                budget=SCOUT_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
            lambda: ctx.agent(
                REPRODUCTION_PROMPT.format(rules=RULES, goal=goal),
                schema=REPRODUCTION_SCHEMA,
                label="reproduction-scout",
                tools=_read_tools(),
                budget=SCOUT_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
        ]
    )
    root_cause = _dict_or(scouts[0] if scouts else None, _empty_root_cause())
    reproduction = _dict_or(
        scouts[1] if len(scouts) > 1 else None,
        _empty_reproduction(),
    )
    evidence = _bounded_json({"root_cause": root_cause, "reproduction": reproduction})

    await ctx.phase("implement")
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=RULES,
            goal=goal,
            root_cause=_bounded_json(root_cause),
            reproduction=_bounded_json(reproduction),
        ),
        label="lead-coder",
        tools=_coder_tools(),
        budget=LEAD_CODER_BUDGET,
        timeout=coder_role_timeout_seconds(),
    )
    source_changed = await _source_diff_present(ctx, injected_paths)
    backup_used = False
    effective_coder_report = coder_report or ""
    if source_changed is False:
        backup_used = True
        backup_report = await ctx.agent(
            BACKUP_CODER_PROMPT.format(
                rules=RULES,
                goal=goal,
                evidence=evidence,
            ),
            label="backup-coder",
            tools=_coder_tools(),
            budget=BACKUP_CODER_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
        effective_coder_report = backup_report or ""
        source_changed = await _source_diff_present(ctx, injected_paths)
    if source_changed is False:
        return {
            "status": "incomplete",
            "reason": "no_source_diff",
            "backup_used": backup_used,
            "tokens_spent": ctx.tokens_spent(),
        }

    if ctx.time_low():
        return {
            "status": "done",
            "internal_verdict": "verification_deferred_time_low",
            "backup_used": backup_used,
            "tokens_spent": ctx.tokens_spent(),
        }

    await ctx.phase("critic")
    critic_evidence = _bounded_json(
        {
            "root_cause": root_cause,
            "reproduction": reproduction,
            "coder_report": effective_coder_report,
        }
    )
    correctness, compatibility = await _critics(
        ctx,
        goal=goal,
        evidence=critic_evidence,
    )
    return {
        "status": "done",
        "internal_verdict": (
            "critics_reported_pass"
            if correctness.get("verdict") == compatibility.get("verdict") == "PASS"
            else "candidate_preserved"
        ),
        "backup_used": backup_used,
        "critics": {"correctness": correctness, "compatibility": compatibility},
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["evidence_action_council_v1"]
