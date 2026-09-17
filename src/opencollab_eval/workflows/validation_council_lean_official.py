"""Lean validation council hosted entirely in an official OC candidate workspace."""

from __future__ import annotations

from typing import Any

from opencollab.workflows import workflow

from ._validation_council_lean_official_impl import (
    run_validation_council_lean_candidate,
)

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


@workflow(
    name="validation-council-lean-official-v1",
    description=(
        "Lean contract-led council executed in an official OpenCollab candidate workspace"
    ),
    phases=["candidate-council", "adoption"],
)
async def validation_council_lean_official_v1(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run the unchanged council graph in isolation, then adopt its final diff."""
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("lean-council-candidate")
    try:
        candidate = await ctx.candidate_workflow(
            run_validation_council_lean_candidate,
            _blind_args(args),
            label="validation-council-lean-official",
            budget=None,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"lean council candidate unavailable after {type(exc).__name__}")
        return {
            "status": "incomplete",
            "candidate_status": "exception",
            "candidate_adopted": False,
            "tokens_spent": ctx.tokens_spent(),
        }

    result = candidate.output if isinstance(candidate.output, dict) else {}
    if not candidate.diff.strip():
        return {
            **result,
            "status": "incomplete" if result.get("status") != "error" else "error",
            "candidate_status": result.get("status", "empty"),
            "candidate_adopted": False,
            "candidate_diff_bytes": 0,
            "tokens_spent": ctx.tokens_spent(),
        }

    await ctx.phase("lean-council-adoption")
    preserve_paths = [
        str(path)
        for path in args.get("injected_test_paths") or []
        if str(path)
    ]
    try:
        await ctx.adopt_candidate(candidate, preserve_paths=preserve_paths)
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"lean council candidate adoption failed after {type(exc).__name__}")
        return {
            **result,
            "status": "incomplete",
            "candidate_status": result.get("status", "unknown"),
            "candidate_adopted": False,
            "candidate_diff_bytes": len(candidate.diff.encode("utf-8")),
            "tokens_spent": ctx.tokens_spent(),
        }

    return {
        **result,
        "candidate_status": result.get("status", "unknown"),
        "candidate_adopted": True,
        "candidate_diff_bytes": len(candidate.diff.encode("utf-8")),
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["validation_council_lean_official_v1"]
