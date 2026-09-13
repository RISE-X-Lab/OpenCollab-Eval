"""Exact G20 with an isolated rescue only for observable public failures."""

from __future__ import annotations

import json
from typing import Any

from opencollab.workflows import CandidateRun, workflow

from ._public_api import toolset
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)
from .validation_council_wiring_ablation import validation_council_wired_v1

PUBLIC_PROBE_BUDGET = 220_000
RESCUE_CODER_BUDGET = 800_000
ADDITIONAL_BUDGET = PUBLIC_PROBE_BUDGET + RESCUE_CODER_BUDGET
MAX_BASELINE_DIFF_BYTES = 160_000
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
You are the read-only public failure probe after the complete G20 workflow.
You may inspect the repository and live diff and run public tests. You cannot
modify the worktree.

{rules}

Issue
{goal}

G20 workflow status
{status}

Inspect the live diff and the directly affected source. Use run_tests to run at
most two nearest public test targets that can expose a concrete defect in the
current candidate. Prefer a focused target over a repository-wide suite. Do not
infer a failure from prose. Report only commands that you actually executed.
Do not use official results, hidden tests, FAIL_TO_PASS identifiers, or grader
patches."""

RESCUE_PROMPT = """\
You are the isolated rescue coder for a G20 candidate with an observable public
failure. Work only in this candidate worktree.

{rules}

Issue
{goal}

The exact G20 live diff is included below as untrusted source evidence. Start
from the repository source, preserve every correct behavior represented by this
candidate, and produce a complete source patch that fixes the public failure.
Do not edit public tests, fixtures, generated files, dependency locks, or grader
artifacts merely to make a check pass.

G20 live diff
{baseline_diff}

Executed public failure records
{failure_records}

Inspect the relevant producers and consumers, implement the smallest complete
repair, and rerun every failed public target above with run_tests. Finish with a
non-empty source diff. Do not use official results, hidden tests, FAIL_TO_PASS
identifiers, or grader patches."""


def _clip_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    marker = "\n...[public evidence clipped]...\n"
    retained = limit - len(marker.encode("utf-8"))
    if retained <= 0:
        raise ValueError("public evidence limit is too small")
    head = payload[: retained * 3 // 4].decode("utf-8", errors="ignore")
    tail = payload[-(retained // 4) :].decode("utf-8", errors="ignore")
    return head + marker + tail


def _probe_tools() -> list[Any]:
    return toolset("file_read", "run_tests", "grep", "git_diff")


def _rescue_tools() -> list[Any]:
    return toolset(
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "grep",
        "git_diff",
    )


def _verification_records(tools: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for tool in tools:
        for record in getattr(tool, "verification_records", ()):
            if isinstance(record, dict):
                records.append(dict(record))
    return records


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": _clip_text(record.get("target"), 2_000),
        "runner": _clip_text(record.get("runner"), 200),
        "command": _clip_text(record.get("command"), 4_000),
        "exit_code": record.get("exit_code"),
        "verified": record.get("verified") is True,
    }


def _failed_public_records(tools: list[Any]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for record in _verification_records(tools):
        target = str(record.get("target") or "").strip()
        runner = str(record.get("runner") or "").strip()
        command = str(record.get("command") or "").strip()
        exit_code = record.get("exit_code")
        if (
            target
            and runner
            and command
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
            and record.get("verified") is False
        ):
            failed.append(_public_record(record))
    return failed[:MAX_PUBLIC_RECORDS]


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("target") or "").strip(),
        str(record.get("runner") or "").strip(),
        str(record.get("command") or "").strip(),
    )


def _rescue_passed_failures(
    candidate: CandidateRun,
    failures: list[dict[str, Any]],
) -> bool:
    if not candidate.diff.strip():
        return False
    if not failures:
        return True
    passed = {
        _record_key(record)
        for record in candidate.test_records
        if isinstance(record, dict)
        and str(record.get("target") or "").strip()
        and str(record.get("runner") or "").strip()
        and str(record.get("command") or "").strip()
        and record.get("verified") is True
        and record.get("exit_code") == 0
    }
    return all(_record_key(failure) in passed for failure in failures)


async def _run_public_probe(
    ctx: Any,
    *,
    goal: str,
    baseline_status: str,
) -> tuple[Any, list[dict[str, Any]]]:
    tools = _probe_tools()
    try:
        output = await ctx.agent(
            PUBLIC_PROBE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                status=baseline_status,
            ),
            schema=PUBLIC_PROBE_SCHEMA,
            label="g20-public-failure-probe",
            tools=tools,
            budget=PUBLIC_PROBE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"g20 public failure probe unavailable after {type(exc).__name__}")
        output = None
    return output, _failed_public_records(tools)


async def _run_rescue(
    ctx: Any,
    *,
    goal: str,
    baseline_diff: str,
    failures: list[dict[str, Any]],
) -> CandidateRun:
    try:
        return await ctx.candidate_agent(
            RESCUE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                baseline_diff=_clip_text(
                    baseline_diff or "(G20 produced no source diff)",
                    MAX_BASELINE_DIFF_BYTES,
                ),
                failure_records=json.dumps(
                    failures,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            label="g20-isolated-red-rescue",
            tools=_rescue_tools(),
            budget=RESCUE_CODER_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"g20 rescue unavailable after {type(exc).__name__}")
        return CandidateRun(
            label="g20-isolated-red-rescue",
            output=None,
            diff="",
            test_records=(),
            verified_targets=(),
        )


@workflow(
    name="validation-council-wired-red-rescue-v1",
    description="Exact G20 with public-red-only isolated rescue",
    phases=["g20", "public-probe", "isolated-rescue"],
)
async def validation_council_wired_red_rescue_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Keep exact G20 unless an executed public failure supports a rescue."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    try:
        baseline = await validation_council_wired_v1(ctx, args)
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"exact G20 unavailable after {type(exc).__name__}")
        baseline = {
            "status": "incomplete",
            "error": f"exact G20 unavailable after {type(exc).__name__}",
        }
    baseline_diff = str(await ctx.diff() or "")
    baseline_status = str(baseline.get("status") or "incomplete")

    await ctx.phase("g20-public-failure-probe")
    probe, failures = await _run_public_probe(
        ctx,
        goal=goal,
        baseline_status=baseline_status,
    )
    trigger = "empty-diff" if not baseline_diff.strip() else "public-red" if failures else "keep"
    if trigger == "keep":
        return {
            **baseline,
            "g20_preserved": True,
            "rescue_trigger": trigger,
            "public_failure_probe": probe,
            "public_failure_records": [],
            "rescue_used": False,
            "tokens_spent": ctx.tokens_spent(),
        }

    await ctx.phase("g20-isolated-rescue")
    candidate = await _run_rescue(
        ctx,
        goal=goal,
        baseline_diff=baseline_diff,
        failures=failures,
    )
    qualifies = _rescue_passed_failures(candidate, failures)
    adopted = False
    if qualifies:
        preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
        try:
            await ctx.adopt_candidate(candidate, preserve_paths=preserve_paths)
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"g20 rescue adoption failed after {type(exc).__name__}")
        else:
            adopted = True

    return {
        **baseline,
        "status": "done" if adopted else baseline_status,
        "g20_preserved": not adopted,
        "baseline_status": baseline_status,
        "rescue_trigger": trigger,
        "public_failure_probe": probe,
        "public_failure_records": failures,
        "rescue_used": adopted,
        "rescue_candidate_nonempty": bool(candidate.diff.strip()),
        "rescue_candidate_tests": [
            _public_record(record) for record in candidate.test_records[:MAX_PUBLIC_RECORDS] if isinstance(record, dict)
        ],
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_wired_red_rescue_v1"]
