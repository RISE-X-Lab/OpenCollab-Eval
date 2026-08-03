"""Tests for the validation-council workflow orchestration."""

from __future__ import annotations

from typing import Any

import pytest

from opencollab_eval.workflows.validation_council_solve import (
    validation_council_solve as run_validation_council_solve,
)


@pytest.fixture(scope="module")
def validation_council_solve():
    return run_validation_council_solve


class ScriptedCtx:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.agent_failures: tuple[dict[str, Any], ...] = ()
        self.source_diff_checks: list[list[str]] = []

    def tokens_spent(self) -> int:
        return 123

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False, **kwargs):
        self.agent_calls.append(
            {"prompt": prompt, "schema": schema, "label": label, "tools": tools, **kwargs}
        )
        return self._replies.pop(0)

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)

    async def source_changed(self, exclude_paths: list[str]) -> bool:
        self.source_diff_checks.append(list(exclude_paths))
        return len(self.source_diff_checks) > 1


class NoSourceDiffCtx(ScriptedCtx):
    async def source_changed(self, exclude_paths: list[str]) -> bool:
        self.source_diff_checks.append(list(exclude_paths))
        return False


class UnknownSourceDiffCtx(ScriptedCtx):
    async def source_changed(self, exclude_paths: list[str]) -> None:
        self.source_diff_checks.append(list(exclude_paths))
        return False if len(self.source_diff_checks) == 1 else None


class PollutedBaselineCtx(ScriptedCtx):
    async def source_changed(self, exclude_paths: list[str]) -> bool:
        self.source_diff_checks.append(list(exclude_paths))
        return True


LOCALIZATION = {
    "summary": "empty widget crashes",
    "root_cause_hypothesis": "parse misses empty input",
    "files": ["widget.py"],
    "public_api": ["widget.parse"],
    "uncertainties": [],
    "definition_of_done": "empty input returns an empty widget",
}
CONTRACTS = {
    "contracts": [{
        "id": "C1",
        "statement": "empty input is accepted",
        "scope": "widget.parse",
        "behavior_kind": "desired",
        "evidence": [{
            "source_type": "issue",
            "file_or_section": "problem statement",
            "summary": "user reports empty input crash",
        }],
        "confidence": "medium",
        "testability": "direct function call",
    }]
}
CARTOGRAPHY = {
    "framework": "pytest",
    "runner_commands": ["pytest tests/test_widget.py"],
    "test_files": ["tests/test_widget.py"],
    "fixtures": [],
    "assertion_style": "plain assert",
    "temporary_test_guidance": "use python -c probes",
}
CANDIDATES = {
    "tests": [{
        "id": "T1",
        "contract_ids": ["C1"],
        "type": "repro",
        "oracle_type": "return value",
        "setup": "call parse('')",
        "assertion": "returns empty widget",
        "expected_on_base": "fail",
        "expected_on_patch": "pass",
        "why_distinguishes_wrong_patch": "catches empty-input crash",
        "evidence_refs": ["C1"],
        "runner_command": "python -c \"import widget; widget.parse('')\"",
        "risk_of_false_positive": "low",
    }],
    "abstained": False,
    "rationale": "direct repro",
}
JUDGE = {
    "accepted": [{
        "id": "T1",
        "priority": 1,
        "classification": "repro",
        "reason": "contract backed",
    }],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}
TRIAGE = {
    "classifications": [{
        "test_id": "T1",
        "status": "base_fail_repro",
        "evidence": "raises ValueError",
    }],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}


def _replies(*coder_replies: Any) -> list[Any]:
    return [LOCALIZATION, CONTRACTS, CARTOGRAPHY, CANDIDATES, JUDGE, TRIAGE, *coder_replies]


async def test_nonempty_candidate_goes_directly_to_official_eval(validation_council_solve):
    ctx = ScriptedCtx(_replies("changed widget.py", "unused internal verdict"))

    result = await validation_council_solve(
        ctx,
        {
            "description": "fix empty widget",
            "fail_to_pass": ["tests/hidden.py::test_secret"],
            "injected_test_paths": ["tests/hidden.py"],
        },
    )

    assert result["status"] == "done"
    assert result["candidate_ready"] is True
    assert result["verification"] == "official_eval_pending"
    assert result["rounds"] == 1
    assert result["contracts"] == 1
    assert result["pre_validation_accepted"] == 1
    assert result["tokens_spent"] == 123
    assert ctx.source_diff_checks == [["tests/hidden.py"], ["tests/hidden.py"]]
    assert ctx._replies == ["unused internal verdict"]
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
    ]
    assert ctx.phases == ["localize", "evidence", "pre-validate", "solve:r1"]
    assert not any(
        "validator" in call["label"] or "verifier" in call["label"]
        for call in ctx.agent_calls
    )
    all_prompts = "\n".join(call["prompt"] for call in ctx.agent_calls)
    assert "tests/hidden.py::test_secret" not in all_prompts

    coder_call = next(call for call in ctx.agent_calls if call["label"] == "coder:r1")
    coder_prompt = coder_call["prompt"]
    assert "widget.py" in coder_prompt
    assert "base_fail_repro" in coder_prompt
    assert "run focused public tests" in coder_prompt
    assert "raw ---/+++/@@ text" in coder_prompt
    assert len(coder_prompt.encode()) < 2_000
    coder_tools = {tool.name for tool in coder_call["tools"]}
    assert {"run_tests", "git_diff", "file_read", "apply_patch"} <= coder_tools


async def test_coder_provider_failure_without_diff_is_technical(validation_council_solve):
    class FailingCoderCtx(NoSourceDiffCtx):
        coder_spent = False

        def tokens_spent(self) -> int:
            return 124 if self.coder_spent else 123

        async def agent(self, prompt, *, label=None, **kwargs):
            result = await super().agent(prompt, label=label, **kwargs)
            if label == "coder:r1":
                self.coder_spent = True
                self.agent_failures = ({
                    "label": label,
                    "exception_type": "APITimeoutError",
                    "status_code": 408,
                    "provider_error_type": None,
                },)
            return result

    ctx = FailingCoderCtx(_replies(None))

    with pytest.raises(RuntimeError, match="coder session failed before producing a source diff"):
        await validation_council_solve(ctx, {"description": "fix empty widget"})


@pytest.mark.parametrize("provider_failed", [False, True])
async def test_unknown_source_diff_is_technical(
    validation_council_solve,
    provider_failed,
):
    class Context(UnknownSourceDiffCtx):
        async def agent(self, prompt, *, label=None, **kwargs):
            result = await super().agent(prompt, label=label, **kwargs)
            if provider_failed and label == "coder:r1":
                self.agent_failures = ({
                    "label": label,
                    "exception_type": "APITimeoutError",
                    "status_code": 408,
                },)
            return result

    ctx = Context(_replies("partial coder report" if provider_failed else "coder report"))

    with pytest.raises(RuntimeError, match="source diff probe did not return"):
        await validation_council_solve(ctx, {"description": "fix empty widget"})

    assert ctx.source_diff_checks == [[], []]


async def test_pre_coder_source_change_is_a_technical_failure(validation_council_solve):
    ctx = PollutedBaselineCtx(_replies("coder must not start"))

    with pytest.raises(RuntimeError, match="pre-coder roles changed the source worktree"):
        await validation_council_solve(
            ctx,
            {"description": "fix empty widget", "injected_test_paths": ["tests/hidden.py"]},
        )

    assert ctx.source_diff_checks == [["tests/hidden.py"]]
    assert "coder:r1" not in [call["label"] for call in ctx.agent_calls]


async def test_every_role_receives_the_complete_public_task_specification(
    validation_council_solve,
):
    goal = (
        "# Public issue\n"
        + "Problem evidence. " * 80
        + "\n\nRequirements:\nREQUIREMENT_SENTINEL must remain visible."
        + "\n\nNew interfaces introduced:\nINTERFACE_SENTINEL must remain visible."
    )
    ctx = ScriptedCtx(_replies("changed widget.py"))

    await validation_council_solve(ctx, {"goal": goal})

    assert ctx.agent_calls
    assert all(goal in call["prompt"] for call in ctx.agent_calls)


async def test_role_timeouts_leave_room_for_provider_retry(
    validation_council_solve,
    monkeypatch,
):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "1800")
    ctx = ScriptedCtx(_replies("changed widget.py"))

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert timeouts["coder:r1"] == 1860
    assert timeouts["analyst-localizer"] == 1860
    assert timeouts["baseline-triage"] == 1860


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
async def test_role_timeouts_reject_invalid_provider_timeout(
    validation_council_solve,
    monkeypatch,
    value,
):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", value)
    ctx = ScriptedCtx(_replies("changed widget.py"))

    with pytest.raises(ValueError, match="OPENCOLLAB_LLM_TIMEOUT"):
        await validation_council_solve(ctx, {"goal": "fix empty widget"})


async def test_empty_coder_diff_retries_without_model_validators(validation_council_solve):
    ctx = NoSourceDiffCtx(_replies(
        "coder produced no source changes",
        "still unchanged",
        "unchanged again",
    ))

    result = await validation_council_solve(
        ctx,
        {"goal": "fix empty widget", "injected_test_paths": ["tests/injected.py"]},
    )

    assert result["status"] == "incomplete"
    assert result["candidate_ready"] is False
    assert result["rounds"] == 3
    assert ctx.source_diff_checks == [["tests/injected.py"]] * 4
    assert [call["label"] for call in ctx.agent_calls][-3:] == [
        "coder:r1",
        "coder:r2",
        "coder:r3",
    ]
    assert all(attempt["candidate_ready"] is False for attempt in result["attempts"])
    retry_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r2")
    assert "completed without a source change" in retry_prompt
    assert len(retry_prompt.encode()) < 2_000


async def test_empty_pre_validation_skips_baseline_executor(validation_council_solve):
    empty_judge = {
        "accepted": [],
        "rejected": [],
        "diagnostic": [],
        "validation_brief": "No accepted probes.",
    }
    ctx = ScriptedCtx([
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        {**CANDIDATES, "tests": [], "abstained": True},
        empty_judge,
        "changed widget.py",
    ])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert "baseline-triage" not in [call["label"] for call in ctx.agent_calls]


async def test_missing_goal_is_an_error_before_any_agent(validation_council_solve):
    ctx = ScriptedCtx([])

    result = await validation_council_solve(ctx, {})

    assert result["status"] == "error"
    assert ctx.agent_calls == []


async def test_zero_call_localizer_failure_stops_before_other_roles(validation_council_solve):
    ctx = ScriptedCtx([None])

    with pytest.raises(
        RuntimeError,
        match="analyst-localizer completed without a successful model response",
    ):
        await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert [call["label"] for call in ctx.agent_calls] == ["analyst-localizer"]


def test_discovery_registers_validation_council_workflow():
    assert run_validation_council_solve.__workflow_spec__.name == "validation-council-solve"
