"""Minimal single-pass analyst, coder, and verifier workflow."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import workflow

from .base_team import (
    ANALYST_PROMPT,
    ANALYST_SCHEMA,
    CODER_PROMPT,
    SHARED_RULES,
    TESTER_PROMPT,
    TESTER_SCHEMA,
    _coder_tools,
    _dump,
    _read_tools,
    _tester_tools,
)


@workflow(
    name="base-team-single-pass-v1",
    description="One analyst, one coder, and one verifier session.",
    phases=["analyze", "code", "verify"],
)
async def base_team_single_pass_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("analyze")
    brief = await ctx.agent(
        ANALYST_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=ANALYST_SCHEMA,
        label="baseTeamSingle:analyst",
        tools=_read_tools(),
    )
    if not isinstance(brief, dict):
        return {
            "status": "incomplete",
            "reason": "analyst produced no structured brief",
            "tokens_spent": ctx.tokens_spent(),
        }

    await ctx.phase("code")
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            brief=_dump(brief),
            findings="No previous verifier findings. This is the only coding pass.",
        ),
        label="baseTeamSingle:coder",
        tools=_coder_tools(),
    )
    source_changed = await ctx.source_changed()

    await ctx.phase("verify")
    verdict = await ctx.agent(
        TESTER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            brief=_dump(brief),
            coder_report=coder_report or "Coder produced no final report.",
        ),
        schema=TESTER_SCHEMA,
        label="baseTeamSingle:verifier",
        tools=_tester_tools(),
    )
    normalized = (
        verdict
        if isinstance(verdict, dict)
        else {
            "verdict": "FAIL",
            "findings": "Verifier produced no structured verdict.",
        }
    )
    verdict_name = str(normalized.get("verdict") or "").upper()
    if verdict_name == "BLOCKED":
        status = "blocked"
    elif verdict_name == "PASS" and source_changed is not False:
        status = "done"
    else:
        status = "incomplete"
    return {
        "status": status,
        "root_cause": brief.get("root_cause", ""),
        "files": brief.get("files", []),
        "coder_report": coder_report or "",
        "source_changed": source_changed,
        "verifier": normalized,
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["base_team_single_pass_v1"]
