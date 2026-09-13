"""Exact G20 versus a minimal coder with mechanical public red-green replay."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from opencollab.tools import VerificationTool, builtin_tools
from opencollab.workflows import CandidateRun, workflow

from . import validation_council_dual_coder_contract as coders
from . import validation_council_g20_coder_red_green as v1
from . import validation_council_wired_dual_g20 as dual
from ._validation_council_solve_defs import (
    SHARED_RULES,
    _complete_goal,
    coder_role_timeout_seconds,
)

CANDIDATE_BUDGET = 3_200_000
MINIMAL_CODER_BUDGET = 3_000_000
TOTAL_RUNNER_BUDGET = 2 * CANDIDATE_BUDGET
MECHANICAL_PROBE_TIMEOUT = 300.0
_SHARED_PROBE_KEY = "g20_coder_red_green_v2_public_probe"


class _MechanicalRunTests:
    """Public run_tests wrapper with fixed mechanical-execution boundaries."""

    name = "run_tests"
    description = "Run one exact public target and runner mechanically."
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "runner": {"type": "string"},
            "timeout": {"type": "number"},
        },
        "required": ["target", "runner"],
        "additionalProperties": False,
    }
    default_timeout = MECHANICAL_PROBE_TIMEOUT
    disable_outer_timeout = False

    def __init__(self) -> None:
        self._delegate = builtin_tools("run_tests", headless=False)[0]

    @property
    def verified_targets(self) -> frozenset[str]:
        return frozenset(getattr(self._delegate, "verified_targets", ()))

    @property
    def verification_records(self) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(record) for record in getattr(self._delegate, "verification_records", ()) if isinstance(record, dict)
        )

    def to_openai_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        unsupported = set(params) - {"target", "runner", "timeout"}
        if unsupported:
            raise ValueError(f"mechanical run_tests received unsupported fields {sorted(unsupported)}")
        environment = getattr(runtime, "environment", None)
        if not bool(getattr(environment, "process_isolated", False)):
            raise RuntimeError("mechanical run_tests requires process isolation")
        target = str(params.get("target") or "").strip()
        runner = str(params.get("runner") or "").strip()
        if not target or not runner:
            raise ValueError("mechanical run_tests requires target and runner")
        result = await self._delegate.execute_with_runtime(
            {
                "target": target,
                "runner": runner,
                "timeout": params.get("timeout", MECHANICAL_PROBE_TIMEOUT),
            },
            runtime,
        )
        if not isinstance(result, str):
            raise TypeError("mechanical run_tests must return text")
        return result


def _new_run_tests() -> VerificationTool:
    tool = _MechanicalRunTests()
    if not isinstance(tool, VerificationTool):
        raise TypeError("public run_tests tool does not satisfy VerificationTool")
    return cast(VerificationTool, tool)


def _valid_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    target = str(value.get("target") or "").strip()
    runner = str(value.get("runner") or "").strip()
    command = str(value.get("command") or "").strip()
    if not target or not runner or not command:
        return {}
    return {"target": target, "runner": runner, "command": command}


def _reference_from_records(records: Any) -> dict[str, str]:
    if not isinstance(records, list):
        return {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = dual._record_key(record)
        exit_code = record.get("exit_code")
        if (
            all(key)
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and isinstance(record.get("verified"), bool)
        ):
            return {"target": key[0], "runner": key[1], "command": key[2]}
    return {}


async def _run_mechanical_probe(
    ctx: Any,
    reference: Mapping[str, object],
) -> tuple[list[dict[str, Any]], str | None]:
    normalized = _valid_reference(reference)
    if not normalized:
        return [], "missing-reference"
    try:
        tool = _new_run_tests()
        await ctx.execute_verification(
            tool,
            {
                "target": normalized["target"],
                "runner": normalized["runner"],
                "timeout": MECHANICAL_PROBE_TIMEOUT,
            },
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"mechanical public probe unavailable after {type(exc).__name__}")
        return [], type(exc).__name__
    records = [dual._public_record(record) for record in tool.verification_records if isinstance(record, dict)]
    if len(records) != 1:
        return [], "unexpected-record-count"
    return records, None


async def _g20_candidate_workflow(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    output = await dual._exact_g20_candidate_workflow(ctx, args)
    result = dict(output) if isinstance(output, dict) else {"g20_result": output}
    reference = _reference_from_records(result.get("public_test_records"))
    records, error = await _run_mechanical_probe(ctx, reference)
    command = str(records[0].get("command") or "").strip() if records else str(reference.get("command") or "").strip()
    result.update(
        {
            "public_command": command,
            "public_test_records": records,
            "mechanical_probe_reference": reference,
            "mechanical_probe_error": error,
        }
    )
    return result


async def _g20_candidate(ctx: Any, args: dict[str, Any]) -> CandidateRun:
    try:
        return await ctx.candidate_workflow(
            _g20_candidate_workflow,
            v1._blind_args(args),
            label="g20-coder-red-green-v2-a",
            budget=CANDIDATE_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"G20 candidate unavailable after {type(exc).__name__}")
        return v1._empty_candidate("g20-coder-red-green-v2-a")


async def _minimal_candidate_workflow(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    reference = _valid_reference(args.get(_SHARED_PROBE_KEY))
    coder_output: Any = None
    coder_failure: str | None = None
    try:
        coder_output = await ctx.agent(
            v1.MINIMAL_CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                public_command=reference.get("command") or "(no shared public command)",
            ),
            label="g20-coder-red-green-v2-b-coder",
            tools=coders._coder_tools(),
            budget=MINIMAL_CODER_BUDGET,
            timeout=coder_role_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        coder_failure = type(exc).__name__
        await ctx.log(f"minimal coder unavailable after {coder_failure}")
    records, probe_error = await _run_mechanical_probe(ctx, reference)
    command = str(records[0].get("command") or "").strip() if records else str(reference.get("command") or "").strip()
    return {
        "coder_output": coder_output,
        "coder_failure": coder_failure,
        "public_command": command,
        "public_test_records": records,
        "mechanical_probe_reference": reference,
        "mechanical_probe_error": probe_error,
    }


async def _minimal_candidate(
    ctx: Any,
    args: dict[str, Any],
    *,
    reference: dict[str, str],
) -> CandidateRun:
    candidate_args = v1._blind_args(args)
    candidate_args[_SHARED_PROBE_KEY] = dict(reference)
    try:
        return await ctx.candidate_workflow(
            _minimal_candidate_workflow,
            candidate_args,
            label="g20-coder-red-green-v2-b",
            budget=CANDIDATE_BUDGET,
        )
    except Exception as exc:  # noqa: BLE001
        await ctx.log(f"minimal coder candidate unavailable after {type(exc).__name__}")
        return v1._empty_candidate("g20-coder-red-green-v2-b")


@workflow(
    name="validation-council-g20-coder-red-green-v2",
    description="Exact G20 and minimal coder with mechanical shared public test replay",
    phases=[
        "g20-candidate",
        "g20-mechanical-probe",
        "minimal-coder",
        "minimal-mechanical-probe",
        "mechanical-selection",
        "adoption",
    ],
)
async def validation_council_g20_coder_red_green_v2(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Keep exact G20 unless one mechanically replayed public test proves B."""
    goal = _complete_goal(str(args.get("goal") or args.get("description") or ""))
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("g20-coder-red-green-v2-a")
    candidate_a = await _g20_candidate(ctx, args)
    records_a = dual._candidate_records(candidate_a)
    reference = _reference_from_records(records_a)

    await ctx.phase("g20-coder-red-green-v2-b")
    candidate_b = await _minimal_candidate(
        ctx,
        args,
        reference=reference,
    )

    await ctx.phase("g20-coder-red-green-v2-selection")
    winner, reason = v1._choose(candidate_a, candidate_b)
    candidates = {"A": candidate_a, "B": candidate_b}
    preserve_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]
    adopted, adoption_attempts = await v1._adopt(
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
        "shared_public_probe": reference,
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


__all__ = ["validation_council_g20_coder_red_green_v2"]
