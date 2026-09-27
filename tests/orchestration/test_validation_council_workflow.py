"""Tests for the validation-council workflow orchestration."""

from __future__ import annotations

import json

import pytest

from opencollab_eval.workflows.validation_council_solve import (
    validation_council_solve as run_validation_council_solve,
)
from tests.support.validation_council_workflow_support import (
    CANDIDATES,
    CARTOGRAPHY,
    CONTRACTS,
    FAIL,
    JUDGE,
    LOCALIZATION,
    PASS,
    RISKS,
    TRIAGE,
    ScriptedCtx,
    _base_replies,
)


@pytest.fixture(scope="module")
def validation_council_solve():
    return run_validation_council_solve











BLOCKED = {
    "verdict": "BLOCKED",
    "findings": "dependency unavailable",
    "allowed_patch_paths": [],
    "disallowed_patch_paths": [],
}




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
    assert "Run relevant public tests" in coder_prompt
    assert "Test cartography:" in coder_prompt
    assert "Contracts:" in coder_prompt
    assert CARTOGRAPHY["runner_commands"][0] in coder_prompt
    assert CONTRACTS["contracts"][0]["statement"] in coder_prompt
    validation = json.loads(coder_prompt.split("Pre-patch validation:\n")[1].split("\n\nBaseline triage:")[0])
    assert validation["accepted_candidates"][0]["runner_command"] == CANDIDATES["tests"][0]["runner_command"]



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


async def test_retry_feedback_preserves_complete_failure(validation_council_solve):
    long_failure = {**FAIL, "findings": "specific failure " * 200}
    ctx = ScriptedCtx(_base_replies(long_failure) + _base_replies(PASS)[6:])

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    retry_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r2")
    assert long_failure["findings"] in retry_prompt


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


def test_discovery_registers_validation_council_workflow():
    assert run_validation_council_solve.__workflow_spec__.name == "validation-council-solve"
