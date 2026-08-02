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


class NoSourceDiffCtx(ScriptedCtx):
    def __init__(self, replies: list[Any]) -> None:
        super().__init__(replies)
        self.source_diff_checks: list[list[str]] = []

    async def source_changed(self, exclude_paths: list[str]) -> bool:
        self.source_diff_checks.append(list(exclude_paths))
        return False


LOCALIZATION = {
    "summary": "empty widget crashes",
    "root_cause_hypothesis": "parse misses empty input",
    "files": ["widget.py"],
    "public_api": ["widget.parse"],
    "uncertainties": [],
    "definition_of_done": "empty input returns an empty widget",
}

CONTRACTS = {
    "contracts": [
        {
            "id": "C1",
            "statement": "empty input is accepted",
            "scope": "widget.parse",
            "behavior_kind": "desired",
            "evidence": [
                {
                    "source_type": "issue",
                    "file_or_section": "problem statement",
                    "summary": "user reports empty input crash",
                }
            ],
            "confidence": "medium",
            "testability": "direct function call",
        }
    ]
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
    "tests": [
        {
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
        }
    ],
    "abstained": False,
    "rationale": "direct repro",
}

JUDGE = {
    "accepted": [
        {"id": "T1", "priority": 1, "classification": "repro", "reason": "contract backed"}
    ],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}

TRIAGE = {
    "classifications": [
        {"test_id": "T1", "status": "base_fail_repro", "evidence": "raises ValueError"}
    ],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}

RISKS = {
    "risks": [
        {
            "id": "R1",
            "changed_area": "widget.parse",
            "risk": "None handling regresses",
            "contract_ids": ["C1"],
            "suggested_probe": "parse(None)",
            "priority": 1,
        }
    ],
    "summary": "small parser risk",
}

PASS = {
    "verdict": "PASS",
    "findings": "validated",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}
FAIL = {
    "verdict": "FAIL",
    "findings": "edge case still fails",
    "allowed_patch_paths": ["widget.py"],
    "disallowed_patch_paths": [],
}
BLOCKED = {
    "verdict": "BLOCKED",
    "findings": "dependency unavailable",
    "allowed_patch_paths": [],
    "disallowed_patch_paths": [],
}


def _base_replies(final_verdict: dict[str, str] = PASS) -> list[Any]:
    return [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        final_verdict,
    ]


async def test_happy_path_passes_first_round(validation_council_solve):
    ctx = ScriptedCtx(_base_replies())

    result = await validation_council_solve(
        ctx,
        {
            "description": "fix empty widget",
            "fail_to_pass": ["tests/hidden.py::test_secret"],
            "injected_test_paths": ["tests/hidden.py"],
        },
    )

    assert result["status"] == "done"
    assert result["rounds"] == 1
    assert result["contracts"] == 1
    assert result["pre_validation_accepted"] == 1
    assert result["allowed_patch_paths"] == ["widget.py"]
    assert result["disallowed_patch_paths"] == []
    assert result["tokens_spent"] == 123
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
        "patch-validator:r1",
        "diff-risk-auditor:r1",
        "post-validation-factory:r1",
        "post-r1-validation-judge",
        "post-validation-triage:r1",
        "final-verifier:r1",
    ]
    budgets = {call["label"]: call.get("budget") for call in ctx.agent_calls}
    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert budgets["analyst-localizer"] == 220_000
    assert budgets["contract-miner"] == 180_000
    assert budgets["test-cartographer"] == 180_000
    assert budgets["pre-validation-factory"] == 160_000
    assert budgets["pre-validation-judge"] == 100_000
    assert budgets["baseline-triage"] == 180_000
    assert budgets["coder:r1"] is None
    assert budgets["patch-validator:r1"] == 220_000
    assert budgets["diff-risk-auditor:r1"] == 60_000
    assert budgets["post-validation-factory:r1"] == 160_000
    assert budgets["post-r1-validation-judge"] == 100_000
    assert budgets["post-validation-triage:r1"] == 180_000
    assert budgets["final-verifier:r1"] == 220_000
    assert timeouts["coder:r1"] == 1800
    for call in ctx.agent_calls:
        if call["label"] != "coder:r1":
            assert call.get("timeout") == 900
    assert ctx.phases == [
        "localize",
        "evidence",
        "pre-validate",
        "solve:r1",
        "diff-risk:r1",
        "final-verify:r1",
    ]
    all_prompts = "\n".join(call["prompt"] for call in ctx.agent_calls)
    assert "tests/hidden.py::test_secret" not in all_prompts
    assert "Report unavailable probes as not_run" in all_prompts
    assert "do not search for write tools" in all_prompts
    coder_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r1")
    assert "widget.py" in coder_prompt
    assert "Never read a\nwhole file" in coder_prompt
    assert "Every file_read must set offset and limit at most 20" in coder_prompt
    assert "Call exactly\none tool per turn" in coder_prompt
    assert "adjacent 20-line windows instead of searching again" in coder_prompt
    assert "Never repeat a successful\nsearch" in coder_prompt
    assert "raw ---/+++/@@ text" in coder_prompt
    assert "never a Begin Patch" in coder_prompt
    assert "file_write for one unique replacement" in coder_prompt
    assert len(coder_prompt.encode()) < 850


async def test_coder_provider_failure_without_diff_aborts_for_technical_retry(
    validation_council_solve,
):
    class FailingCoderCtx(NoSourceDiffCtx):
        coder_spent = False

        def tokens_spent(self) -> int:
            return 124 if self.coder_spent else 123

        async def agent(self, prompt, *, label=None, **kwargs):
            result = await super().agent(prompt, label=label, **kwargs)
            if label == "coder:r1":
                self.coder_spent = True
                self.agent_failures = (
                    {
                        "label": label,
                        "exception_type": "APITimeoutError",
                        "status_code": 408,
                        "provider_error_type": None,
                    },
                )
            return result

    ctx = FailingCoderCtx([LOCALIZATION, CONTRACTS, CARTOGRAPHY, CANDIDATES, JUDGE, TRIAGE, None])

    with pytest.raises(RuntimeError, match="coder session failed before producing a source diff"):
        await validation_council_solve(ctx, {"description": "fix empty widget"})


async def test_every_role_receives_the_complete_public_task_specification(
    validation_council_solve,
):
    goal = (
        "# Public issue\n"
        + "Problem evidence. " * 80
        + "\n\nRequirements:\nREQUIREMENT_SENTINEL must remain visible."
        + "\n\nNew interfaces introduced:\nINTERFACE_SENTINEL must remain visible."
    )
    assert len(goal.encode()) > 640
    ctx = ScriptedCtx(_base_replies())

    await validation_council_solve(ctx, {"goal": goal})

    assert ctx.agent_calls
    assert all(goal in call["prompt"] for call in ctx.agent_calls)


async def test_failed_final_verifier_retries_with_feedback(validation_council_solve):
    ctx = ScriptedCtx(_base_replies(FAIL) + _base_replies(PASS)[6:])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert "edge case still fails" in ctx.agent_calls[13]["prompt"]
    budgets = {call["label"]: call.get("budget") for call in ctx.agent_calls}
    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert budgets["coder:r2"] is None
    assert budgets["patch-validator:r2"] == 220_000
    assert budgets["diff-risk-auditor:r2"] == 60_000
    assert budgets["post-validation-factory:r2"] == 160_000
    assert budgets["post-r2-validation-judge"] == 100_000
    assert budgets["post-validation-triage:r2"] == 180_000
    assert budgets["final-verifier:r2"] == 220_000
    assert timeouts["coder:r2"] == 1800
    assert timeouts["patch-validator:r2"] == 900
    assert timeouts["diff-risk-auditor:r2"] == 900
    assert timeouts["post-validation-factory:r2"] == 900
    assert timeouts["post-r2-validation-judge"] == 900
    assert timeouts["post-validation-triage:r2"] == 900
    assert timeouts["final-verifier:r2"] == 900
    assert any("attempt 1 failed" in message for message in ctx.logs)


async def test_role_timeouts_leave_room_for_provider_retry(
    validation_council_solve,
    monkeypatch,
):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "1800")
    ctx = ScriptedCtx(_base_replies())

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    timeouts = {call["label"]: call.get("timeout") for call in ctx.agent_calls}
    assert timeouts["coder:r1"] == 1860
    assert timeouts["analyst-localizer"] == 1860
    assert timeouts["final-verifier:r1"] == 1860


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
async def test_role_timeouts_reject_invalid_provider_timeout(
    validation_council_solve,
    monkeypatch,
    value,
):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", value)
    ctx = ScriptedCtx(_base_replies())

    with pytest.raises(ValueError, match="OPENCOLLAB_LLM_TIMEOUT"):
        await validation_council_solve(ctx, {"goal": "fix empty widget"})


async def test_retry_feedback_is_bounded(validation_council_solve):
    long_failure = {**FAIL, "findings": "specific failure " * 200}
    ctx = ScriptedCtx(_base_replies(long_failure) + _base_replies(PASS)[6:])

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    retry_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r2")
    assert "...[shortened]..." in retry_prompt
    assert len(retry_prompt.encode()) < 1200


async def test_coder_prompt_keeps_localized_path_ahead_of_long_prose(
    validation_council_solve,
):
    replies = _base_replies()
    replies[0] = {
        **LOCALIZATION,
        "files": ["openlibrary/solr/update_work.py"],
        "definition_of_done": "two-value return contract " * 100,
    }
    ctx = ScriptedCtx(replies)

    await validation_council_solve(ctx, {"goal": "fix updater return shape"})

    coder_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r1")
    assert "openlibrary/solr/update_work.py" in coder_prompt


async def test_empty_pre_validation_skips_baseline_executor(validation_council_solve):
    empty_judge = {
        "accepted": [],
        "rejected": [],
        "diagnostic": [],
        "validation_brief": "No accepted probes.",
    }
    replies = [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        {**CANDIDATES, "tests": [], "abstained": True},
        empty_judge,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        PASS,
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert "baseline-triage" not in [call["label"] for call in ctx.agent_calls]


async def test_failed_final_verifier_allows_three_coder_rounds(validation_council_solve):
    ctx = ScriptedCtx(_base_replies(FAIL) + _base_replies(FAIL)[6:] + _base_replies(FAIL)[6:])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "incomplete"
    assert result["rounds"] == 3
    labels = [call["label"] for call in ctx.agent_calls]
    assert "coder:r3" in labels
    assert "final-verifier:r3" in labels
    assert not any(label == "coder:r4" for label in labels)
    assert any("attempt 1 failed" in message for message in ctx.logs)
    assert any("attempt 2 failed" in message for message in ctx.logs)
    assert len(result["attempts"]) == 3


async def test_empty_coder_diff_skips_model_validators_and_retries_coder(
    validation_council_solve,
):
    ctx = NoSourceDiffCtx(
        [LOCALIZATION, CONTRACTS, CARTOGRAPHY, CANDIDATES, JUDGE, TRIAGE]
        + ["coder produced no source changes"] * 3
    )

    result = await validation_council_solve(
        ctx,
        {"goal": "fix empty widget", "injected_test_paths": ["tests/injected.py"]},
    )

    labels = [call["label"] for call in ctx.agent_calls]
    assert result["status"] == "incomplete"
    assert result["rounds"] == 3
    assert labels[-3:] == ["coder:r1", "coder:r2", "coder:r3"]
    assert not any(label.startswith("patch-validator:") for label in labels)
    assert ctx.source_diff_checks == [["tests/injected.py"]] * 3
    assert all(
        "no tracked source changes" in attempt["final_verdict"]["findings"]
        for attempt in result["attempts"]
    )


async def test_blocked_patch_validator_short_circuits_retry(validation_council_solve):
    replies = [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        BLOCKED,
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "blocked"
    assert result["rounds"] == 1
    assert result["blocker"] == "dependency unavailable"
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
        "patch-validator:r1",
    ]
    assert any("attempt 1 blocked" in message for message in ctx.logs)


async def test_blocked_final_verifier_short_circuits_retry(validation_council_solve):
    replies = [
        LOCALIZATION,
        CONTRACTS,
        CARTOGRAPHY,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        "changed widget.py",
        PASS,
        RISKS,
        CANDIDATES,
        JUDGE,
        TRIAGE,
        BLOCKED,
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "blocked"
    assert result["rounds"] == 1
    assert result["blocker"] == "dependency unavailable"
    assert [call["label"] for call in ctx.agent_calls][-1] == "final-verifier:r1"
    assert not any(call["label"] == "coder:r2" for call in ctx.agent_calls)


async def test_missing_goal_is_an_error_before_any_agent(validation_council_solve):
    ctx = ScriptedCtx([])

    result = await validation_council_solve(ctx, {})

    assert result["status"] == "error"
    assert ctx.agent_calls == []


async def test_zero_call_localizer_failure_stops_before_other_roles(
    validation_council_solve,
):
    ctx = ScriptedCtx([None])

    with pytest.raises(
        RuntimeError,
        match="analyst-localizer completed without a successful model response",
    ):
        await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert [call["label"] for call in ctx.agent_calls] == ["analyst-localizer"]


def test_discovery_registers_validation_council_workflow():
    assert run_validation_council_solve.__workflow_spec__.name == "validation-council-solve"
