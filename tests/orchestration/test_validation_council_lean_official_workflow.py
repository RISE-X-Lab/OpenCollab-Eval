"""Tests for the validation-council workflow orchestration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from opencollab_eval.workflows._validation_council_lean_official_critic import (
    RISK_CRITIC_SCHEMA,
)
from opencollab_eval.workflows._validation_council_lean_official_critic import (
    normalize_risk_critic as _normalize_risk_critic,
)
from opencollab_eval.workflows._validation_council_lean_official_impl import (
    run_validation_council_lean_candidate as run_validation_council_solve,
)
from opencollab_eval.workflows._validation_council_lean_official_post_patch import (
    POST_PATCH_EVIDENCE_SCHEMA,
    _retry_feedback,
)
from opencollab_eval.workflows.validation_council_lean_official import (
    validation_council_lean_official_v1,
)


@pytest.fixture(scope="module")
def validation_council_solve():
    return run_validation_council_solve


class ScriptedCtx:
    def __init__(
        self,
        replies: list[Any],
        *,
        source_changed: bool = True,
        diff_value: str | None = None,
    ) -> None:
        self._replies = list(replies)
        self._source_changed = source_changed
        self.agent_calls: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.source_exclusions: list[list[str]] = []
        self.diff_value = diff_value

    def tokens_spent(self) -> int:
        return 123

    async def agent(self, prompt, *, schema=None, label=None, tools=None, isolation=False, **kwargs):
        self.agent_calls.append({"prompt": prompt, "schema": schema, "label": label, "tools": tools, **kwargs})
        return self._replies.pop(0)

    async def parallel(self, thunks):
        return [await thunk() for thunk in thunks]

    async def phase(self, title):
        self.phases.append(title)

    async def log(self, message):
        self.logs.append(message)

    async def source_changed(self, exclude_paths):
        self.source_exclusions.append(exclude_paths)
        return self._source_changed

    async def diff(self):
        return self.diff_value


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
    ],
    "coverage_matrix": [
        {
            "case_id": "B1",
            "contract_ids": ["C1"],
            "state_dimensions": "empty input",
            "expected_behavior": "returns an empty widget",
            "relevant_source_path": "widget.py:parse",
            "suggested_probe": "python -c \"import widget; widget.parse('')\"",
            "priority": 1,
        },
        {
            "case_id": "B2",
            "contract_ids": ["C1"],
            "state_dimensions": "normal nonempty input",
            "expected_behavior": "preserves existing parsing",
            "relevant_source_path": "widget.py:parse",
            "suggested_probe": "python -c \"import widget; widget.parse('ok')\"",
            "priority": 2,
        },
        {
            "case_id": "B3",
            "contract_ids": ["C1"],
            "state_dimensions": "adjacent None input",
            "expected_behavior": "preserves the documented None behavior",
            "relevant_source_path": "widget.py:parse",
            "suggested_probe": "python -c \"import widget; widget.parse(None)\"",
            "priority": 3,
        },
    ],
}
CARTOGRAPHY = {
    "framework": "pytest",
    "working_runner_commands": ["pytest tests/test_widget.py"],
    "runner_commands": [
        "pytest tests/test_widget.py",
        "pytest tests/test_widget_regression.py",
        "pytest tests/test_widget_edge.py",
    ],
    "test_files": ["tests/test_widget.py"],
    "fixtures": ["widget_factory"],
    "assertion_style": "plain assert",
    "available_tools": ["python: Python 3.11", "pytest: pytest 8"],
    "unavailable_tools": [],
    "available_dependencies": ["python: pytest import succeeded"],
    "unavailable_dependencies": [],
    "capabilities": ["network unavailable: connection refused; do not install packages"],
    "diagnostics": ["python -m pytest --version: passed (pytest 8)"],
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
    "accepted": [{"id": "T1", "priority": 1, "classification": "repro", "reason": "contract backed"}],
    "rejected": [],
    "diagnostic": [],
    "validation_brief": "run T1 when cheap",
}
TRIAGE = {
    "classifications": [
        {
            "test_id": "T1",
            "status": "base_fail_repro",
            "command": "python -c \"import widget; widget.parse('')\"",
            "observed": True,
            "evidence": "raises ValueError",
            "failure_signature": "",
        }
    ],
    "approved_brief": "T1 is a valid repro",
    "abstained": False,
}


def post_patch_evidence(verdict: str = "PASS") -> dict[str, Any]:
    failing = verdict == "FAIL"
    return {
        "diff": {
            "changed_files": ["widget.py"],
            "unexpected_files": [],
            "summary": "guard empty input",
        },
        "validated_patch_sha256": "",
        "contract_checks": [
            {
                "contract_id": "C1",
                "status": "fail" if failing else "pass",
                "command": "python -c \"import widget; widget.parse('')\"",
                "evidence": "ValueError" if failing else "returned empty widget",
            }
        ],
        "coverage_checks": [
            {
                "case_id": "B1",
                "status": "fail" if failing else "pass",
                "command": "python -c \"import widget; widget.parse('')\"",
                "evidence": "ValueError" if failing else "returned empty widget",
            },
            {
                "case_id": "B2",
                "status": "pass",
                "command": "python -c \"import widget; widget.parse('ok')\"",
                "evidence": "normal parsing preserved",
            },
            {
                "case_id": "B3",
                "status": "pass",
                "command": "python -c \"import widget; widget.parse(None)\"",
                "evidence": "documented None behavior preserved",
            },
        ],
        "baseline_replay": [
            {
                "test_id": "T1",
                "before_status": "base_fail_repro",
                "after_status": "fail" if failing else "pass",
                "command": "pytest tests/test_widget.py",
                "evidence": "failed" if failing else "1 passed",
            }
        ],
        "risk_checks": [],
        "failure_classifications": (
            [
                {
                    "command": "python -c \"import widget; widget.parse('')\"",
                    "classification": "unresolved",
                    "baseline_test_id": "T1",
                    "failure_signature": "ValueError",
                    "evidence": "ValueError",
                },
                {
                    "command": "pytest tests/test_widget.py",
                    "classification": "unresolved",
                    "baseline_test_id": "T1",
                    "failure_signature": "failed",
                    "evidence": "failed",
                },
            ]
            if failing
            else []
        ),
        "remaining_defects": ["empty input still raises"] if failing else [],
        "retry_brief": {
            "must_fix": (
                [
                    {
                        "priority": 1,
                        "problem": "empty input still raises",
                        "evidence": "T1 failed with ValueError",
                        "contract_ids": ["C1"],
                        "failed_command": "pytest tests/test_widget.py",
                        "location": "widget.py:parse",
                        "missing_case": "empty input remains unhandled",
                        "distinguishing_probe": "python -c \"import widget; widget.parse('')\"",
                    }
                ]
                if failing
                else []
            ),
            "do_not_change": ["nonempty parsing"],
        },
        "critic_reason": "cross-module uncertainty" if verdict == "NEEDS_CRITIC" else "",
        "verdict": verdict,
        "findings": "edge case still fails" if failing else "validated",
        "allowed_patch_paths": ["widget.py"],
        "protected_paths": ["existing public tests"],
        "actual_disallowed_changed_files": [],
    }


PASS = post_patch_evidence()
FAIL = post_patch_evidence("FAIL")
BLOCKED = {
    **post_patch_evidence("BLOCKED"),
    "findings": "dependency unavailable",
    "remaining_defects": ["dependency unavailable"],
}
BLOCKED_ACTIONABLE = {
    **post_patch_evidence("FAIL"),
    "verdict": "BLOCKED",
    "findings": "public regression still fails",
}
NEEDS_CRITIC = post_patch_evidence("NEEDS_CRITIC")
CRITIC = {
    "recommendation": "RETRY",
    "risks": [
        {
            "id": "R1",
            "risk": "None handling may regress",
            "contract_ids": ["C1"],
            "missing_case": "None input",
            "source_path_or_unknown": "widget.py:parse",
            "distinguishing_probe": 'python -c "import widget; widget.parse(None)"',
            "probe_status": "not_run",
            "evidence": "None branch is adjacent and uncovered",
            "priority": 1,
        }
    ],
    "coverage_reviews": [],
    "environment_findings": [],
    "evidence_gaps": ["None behavior not checked"],
    "summary": "one grounded edge risk",
}
EMPTY_CRITIC = {
    "recommendation": "PASS",
    "risks": [],
    "coverage_reviews": [],
    "environment_findings": [],
    "evidence_gaps": [],
    "summary": "no uncovered risk",
}
PATCHED_WIDGET_DIFF = (
    "[Working tree status]\n M widget.py\n\n[Patch vs HEAD]\n"
    "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)
PATCHED_WIDGET_SHA256 = "85e9233aa30b9209fd95986e134832088f819ee8d16c7eee136e8e75401e5fe4"


def _prefix_replies() -> list[Any]:
    return [LOCALIZATION, CONTRACTS, CARTOGRAPHY, CANDIDATES, JUDGE, TRIAGE]


def _attempt_replies(
    verifier: dict[str, Any] = PASS,
    *,
    critic: dict[str, Any] = EMPTY_CRITIC,
) -> list[Any]:
    replies = ["changed widget.py", deepcopy(verifier)]
    if verifier.get("verdict") == "NEEDS_CRITIC":
        replies.append(deepcopy(critic))
    return replies


def _base_replies(verifier: dict[str, Any] = PASS) -> list[Any]:
    return [*deepcopy(_prefix_replies()), *_attempt_replies(verifier)]


async def test_happy_path_uses_one_integrated_verifier(validation_council_solve):
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
    assert result["post_patch_checks"] == 5
    assert result["critic_invocations"] == 0
    assert result["tokens_spent"] == 123
    assert [call["label"] for call in ctx.agent_calls] == [
        "analyst-localizer",
        "contract-miner",
        "test-cartographer",
        "pre-validation-factory",
        "pre-validation-judge",
        "baseline-triage",
        "coder:r1",
        "integrated-verifier:r1",
    ]
    assert ctx.phases == ["localize", "evidence", "pre-validate", "solve:r1", "verify:r1"]
    assert ctx.source_exclusions == [["tests/hidden.py"]]
    all_prompts = "\n".join(call["prompt"] for call in ctx.agent_calls)
    assert "tests/hidden.py::test_secret" not in all_prompts
    assert "network unavailable: connection refused" in all_prompts
    verifier_call = next(call for call in ctx.agent_calls if call["label"] == "integrated-verifier:r1")
    assert verifier_call["schema"] is POST_PATCH_EVIDENCE_SCHEMA
    assert verifier_call["budget"] == 440_000
    assert verifier_call["timeout"] == 900


async def test_uncertain_verdict_uses_critic_and_can_retry_coder(validation_council_solve):
    replies = [*_prefix_replies(), *_attempt_replies(NEEDS_CRITIC, critic=CRITIC), *_attempt_replies(PASS)]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert result["critic_invocations"] == 1
    labels = [call["label"] for call in ctx.agent_calls]
    assert labels[-5:] == [
        "coder:r1",
        "integrated-verifier:r1",
        "risk-critic:r1",
        "coder:r2",
        "integrated-verifier:r2",
    ]
    critic_call = next(call for call in ctx.agent_calls if call["label"] == "risk-critic:r1")
    assert critic_call["schema"] is RISK_CRITIC_SCHEMA
    assert "recommendation" in critic_call["schema"]["properties"]
    assert critic_call["budget"] == 80_000
    assert '"case_id":"B1"' in critic_call["prompt"]
    assert ctx.phases[-2:] == ["solve:r2", "verify:r2"]


async def test_concrete_failure_retries_coder_without_critic(validation_council_solve):
    ctx = ScriptedCtx([*_base_replies(FAIL), *_attempt_replies(PASS)])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert result["critic_invocations"] == 0
    labels = [call["label"] for call in ctx.agent_calls]
    assert labels[-4:] == [
        "coder:r1",
        "integrated-verifier:r1",
        "coder:r2",
        "integrated-verifier:r2",
    ]
    retry_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r2")
    assert '"must_fix"' in retry_prompt
    assert "empty input still raises" in retry_prompt
    assert "pytest tests/test_widget.py" in retry_prompt
    assert "empty input remains unhandled" in retry_prompt
    assert "distinguishing_probe" in retry_prompt
    assert any("attempt 1 failed" in message for message in ctx.logs)


async def test_coder_receives_complete_pre_patch_evidence(validation_council_solve):
    contract_detail = "The public API must preserve this behavior. " * 30
    judge_detail = "Accepted because the public test covers the contract. " * 20
    triage_detail = "python -m pytest tests/test_widget.py reached the runner and failed. " * 20
    replies = _base_replies()
    replies[1] = {
        "contracts": [
            *CONTRACTS["contracts"],
            {"id": "C2", "statement": "another requirement"},
            {"id": "C3", "statement": "a third requirement"},
            {"id": "C4", "statement": contract_detail},
        ]
    }
    replies[4] = {**JUDGE, "validation_brief": judge_detail}
    replies[5] = {**TRIAGE, "approved_brief": triage_detail}
    ctx = ScriptedCtx(replies)

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    coder_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r1")
    assert "Contracts:" in coder_prompt
    assert '"id":"C4"' in coder_prompt
    assert contract_detail in coder_prompt
    assert "Pre-patch validation:" in coder_prompt
    assert judge_detail in coder_prompt
    assert "Baseline triage:" in coder_prompt
    assert triage_detail in coder_prompt


async def test_baseline_executor_receives_complete_accepted_probe(validation_council_solve):
    rejected = {
        **CANDIDATES["tests"][0],
        "id": "T2",
        "setup": "REJECTED_SETUP_SENTINEL",
        "assertion": "REJECTED_ASSERTION_SENTINEL",
        "runner_command": "python rejected_probe.py",
    }
    replies = _base_replies()
    replies[3] = {**CANDIDATES, "tests": [CANDIDATES["tests"][0], rejected]}
    ctx = ScriptedCtx(replies)

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    baseline_prompt = next(
        call["prompt"] for call in ctx.agent_calls if call["label"] == "baseline-triage"
    )
    assert "call parse('')" in baseline_prompt
    assert "returns empty widget" in baseline_prompt
    assert '"runner_command":' in baseline_prompt
    assert "widget.parse('')" in baseline_prompt
    assert "why_distinguishes_wrong_patch" in baseline_prompt
    assert "REJECTED_SETUP_SENTINEL" not in baseline_prompt


async def test_every_role_receives_complete_public_task(validation_council_solve):
    goal = "# Public issue\n" + "Problem evidence. " * 80 + "\nREQUIREMENT_SENTINEL"
    ctx = ScriptedCtx(_base_replies())

    await validation_council_solve(ctx, {"goal": goal})

    assert all(goal in call["prompt"] for call in ctx.agent_calls)


async def test_role_timeouts_leave_room_for_provider_retry(validation_council_solve, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", "1800")
    ctx = ScriptedCtx(_base_replies())

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert all(call.get("timeout") == 1860 for call in ctx.agent_calls)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
async def test_role_timeouts_reject_invalid_provider_timeout(validation_council_solve, monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_LLM_TIMEOUT", value)
    ctx = ScriptedCtx(_base_replies())

    with pytest.raises(ValueError, match="OPENCOLLAB_LLM_TIMEOUT"):
        await validation_council_solve(ctx, {"goal": "fix empty widget"})


async def test_retry_feedback_is_bounded(validation_council_solve):
    long_failure = deepcopy(FAIL)
    long_failure["retry_brief"]["must_fix"][0]["evidence"] = "specific failure " * 1000
    ctx = ScriptedCtx([*_base_replies(long_failure), *_attempt_replies(PASS)])

    await validation_council_solve(ctx, {"goal": "fix empty widget"})

    retry_prompt = next(call["prompt"] for call in ctx.agent_calls if call["label"] == "coder:r2")
    assert "...[shortened]..." in retry_prompt
    assert len(_retry_feedback(long_failure).encode()) <= 6000


def test_retry_brief_schema_requires_behavioral_gap_and_distinguishing_probe():
    item_schema = POST_PATCH_EVIDENCE_SCHEMA["properties"]["retry_brief"]["properties"]["must_fix"]["items"]

    assert "missing_case" in item_schema["required"]
    assert "distinguishing_probe" in item_schema["required"]


def test_contract_schema_requires_pre_coder_behavior_matrix(validation_council_solve):
    schema = validation_council_solve.__globals__["CONTRACT_SCHEMA"]
    item = schema["properties"]["coverage_matrix"]["items"]

    assert "coverage_matrix" in schema["required"]
    assert schema["properties"]["coverage_matrix"]["minItems"] == 1
    assert schema["properties"]["coverage_matrix"]["maxItems"] == 5
    assert item["required"] == [
        "case_id",
        "contract_ids",
        "state_dimensions",
        "expected_behavior",
        "relevant_source_path",
        "suggested_probe",
        "priority",
    ]


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
        *_attempt_replies(PASS),
    ]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert "baseline-triage" not in [call["label"] for call in ctx.agent_calls]


def test_unexecuted_baseline_claim_is_downgraded_to_not_run(validation_council_solve):
    normalize = validation_council_solve.__globals__["_normalize_baseline_triage"]

    report = normalize(
        {
            "classifications": [
                {
                    "test_id": "T1",
                    "status": "base_fail_repro",
                    "command": "",
                    "observed": False,
                    "evidence": "static inference only",
                }
            ],
            "approved_brief": "T1 fails on base",
            "abstained": False,
        }
    )

    assert report["classifications"][0]["status"] == "not_run"
    assert report["classifications"][0]["observed"] is False


async def test_failure_allows_three_coder_rounds(validation_council_solve):
    ctx = ScriptedCtx([*_prefix_replies(), *_attempt_replies(FAIL), *_attempt_replies(FAIL), *_attempt_replies(FAIL)])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "incomplete"
    assert result["rounds"] == 3
    labels = [call["label"] for call in ctx.agent_calls]
    assert "coder:r3" in labels
    assert "integrated-verifier:r3" in labels
    assert "coder:r4" not in labels


async def test_no_op_retry_stops_after_repeated_patch_and_feedback(validation_council_solve):
    ctx = ScriptedCtx(
        [*_prefix_replies(), *_attempt_replies(FAIL), *_attempt_replies(FAIL)],
        diff_value=PATCHED_WIDGET_DIFF,
    )

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "incomplete"
    assert result["rounds"] == 2
    assert "coder:r3" not in [call["label"] for call in ctx.agent_calls]
    assert any("stopping no-op retry loop" in message for message in ctx.logs)


async def test_missing_structured_verdict_uses_independent_serializer(validation_council_solve):
    serialized_pass = deepcopy(PASS)
    serialized_pass["validated_patch_sha256"] = PATCHED_WIDGET_SHA256
    replies = [*_prefix_replies(), "changed widget.py", None, serialized_pass]
    ctx = ScriptedCtx(replies, diff_value=PATCHED_WIDGET_DIFF)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    serializer = next(call for call in ctx.agent_calls if call["label"].endswith(":serializer"))
    assert serializer["budget"] == 80_000
    assert serializer["tools"] == []
    assert "No public runtime ledger is available" in serializer["prompt"]


async def test_blocked_verifier_short_circuits_retry(validation_council_solve):
    ctx = ScriptedCtx(_base_replies(BLOCKED))

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "blocked"
    assert result["rounds"] == 1
    assert result["blocker"] == "dependency unavailable"
    assert [call["label"] for call in ctx.agent_calls][-1] == "integrated-verifier:r1"
    assert any("attempt 1 blocked" in message for message in ctx.logs)


async def test_blocked_verifier_with_actionable_evidence_retries_coder(validation_council_solve):
    ctx = ScriptedCtx([*_base_replies(BLOCKED_ACTIONABLE), *_attempt_replies(PASS)])

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "done"
    assert result["rounds"] == 2
    assert "coder:r2" in [call["label"] for call in ctx.agent_calls]
    assert any("BLOCKED but supplied actionable retry evidence" in message for message in ctx.logs)


async def test_missing_conditional_critic_is_inconclusive_not_a_code_failure(validation_council_solve):
    replies = [*_prefix_replies(), "changed widget.py", deepcopy(NEEDS_CRITIC), None]
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "incomplete"
    verdict = result["attempts"][0]["final_verdict"]
    assert verdict["verdict"] == "NEEDS_CRITIC"
    assert "no structured report" in verdict["findings"]


async def test_patch_facts_reject_existing_test_edits_but_ignore_new_probe_tests(
    validation_council_solve,
):
    diff_value = (
        "[Working tree status]\n M widget.py\n M tests/test_widget.py\n?? tests/test_probe.py\n\n"
        "[Patch vs HEAD]\n"
        "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n@@ -1 +1 @@\n-old test\n+changed test\n"
        "diff --git a/tests/test_probe.py b/tests/test_probe.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_probe.py\n@@ -0,0 +1 @@\n+assert True\n"
    )
    facts = await validation_council_solve.__globals__["_patch_facts"](
        ScriptedCtx([], diff_value=diff_value),
        [],
    )

    assert facts["changed_files"] == ["widget.py", "tests/test_widget.py"]
    assert facts["actual_disallowed_changed_files"] == ["tests/test_widget.py"]


def test_risk_critic_is_limited_to_two_grounded_hypotheses():
    report = _normalize_risk_critic(
        {
            "recommendation": "RETRY",
            "risks": [{"id": str(index)} for index in range(6)],
            "coverage_reviews": [],
            "environment_findings": [],
            "evidence_gaps": [],
            "summary": "",
        }
    )

    assert [risk["id"] for risk in report["risks"]] == ["0", "1"]


async def test_critic_retry_does_not_start_a_second_verifier(validation_council_solve):
    replies = [*_prefix_replies(), *_attempt_replies(NEEDS_CRITIC, critic=CRITIC)]
    for _ in range(2):
        replies.extend(_attempt_replies(FAIL))
    ctx = ScriptedCtx(replies)

    result = await validation_council_solve(ctx, {"goal": "fix empty widget"})

    assert result["status"] == "incomplete"
    assert result["attempts"][0]["final_verdict"]["verdict"] == "FAIL"
    assert "one grounded edge risk" in result["attempts"][0]["final_verdict"]["findings"]
    assert result["critic_invocations"] == 1
    assert not any("after-critic" in call["label"] for call in ctx.agent_calls)


async def test_missing_goal_is_an_error_before_any_agent(validation_council_solve):
    ctx = ScriptedCtx([])

    result = await validation_council_solve(ctx, {})

    assert result["status"] == "error"
    assert ctx.agent_calls == []


def test_discovery_registers_validation_council_workflow():
    spec = validation_council_lean_official_v1.__workflow_spec__
    assert spec.name == "validation-council-lean-official-v1"
    assert spec.phases == ("candidate-council", "adoption")
