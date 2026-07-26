"""Execution implementation for :mod:`.swe_committee_v2`."""

from __future__ import annotations

import json
from typing import Any

from opencollab.workflows import workflow

from ._public_api import toolset
from ._swe_committee_v2_defs import (
    BASELINE_JUDGE_PROMPT,
    BASELINE_JUDGE_SCHEMA,
    BASELINE_TRIAGE_PROMPT,
    CANDIDATE_GENERATION_PROMPT,
    CANDIDATE_TESTS_SCHEMA,
    CODER_PROMPT,
    CONTRACT_INVENTORY_PROMPT,
    CONTRACT_MINER_PROMPT,
    CONTRACT_MINER_SCHEMA,
    CONTRACT_TRIBUNAL_PROMPT,
    DIFF_RISK_BRANCH_PROMPT,
    DIFF_RISK_OBSERVABLE_PROMPT,
    DIFF_RISK_REGRESSION_PROMPT,
    DIFF_RISK_SCHEMA,
    EMPTY_DIFF_RISKS,
    EMPTY_FINAL_VERIFIER,
    EMPTY_POST_JUDGE,
    EMPTY_SKEPTIC,
    ETV_PROMPT,
    ETV_SCHEMA,
    FINAL_SKEPTIC_PROMPT,
    FINAL_VERIFIER_PROMPT,
    FINAL_VERIFIER_SCHEMA,
    LOCALIZATION_SCHEMA,
    LOCALIZER_PROMPT,
    MAX_CODER_ROUNDS,
    MAX_POST_TESTS,
    MAX_PRE_TESTS,
    OBSERVABLE_INVENTORY_SCHEMA,
    POST_JUDGE_TRIAGE_PROMPT,
    POST_VALIDATION_DECISION_SCHEMA,
    POST_VALIDATION_FACTORY_PROMPT,
    SHARED_RULES,
    SKEPTIC_SCHEMA,
    TEST_CARTOGRAPHER_PROMPT,
    TEST_CARTOGRAPHY_SCHEMA,
    TRIAGE_SCHEMA,
    TRIBUNAL_SCHEMA,
)


def _read_tools() -> list[Any]:
    return toolset("bash", "file_read", "grep")


def _coder_tools() -> list[Any]:
    return toolset(
        "bash", "file_read", "file_write", "apply_patch", "run_tests", "grep"
    )


def _tester_tools() -> list[Any]:
    return toolset("bash", "file_read", "run_tests", "grep")


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _dict_or(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else fallback


def _trim(judge: dict[str, Any], cap: int) -> dict[str, Any]:
    accepted = judge.get("accepted")
    if isinstance(accepted, list):
        judge = {**judge, "accepted": accepted[:cap]}
    return judge


def _is_pass(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "PASS"


def _post_failed_with_evidence(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "FAIL_WITH_EVIDENCE"


def _contract_evidence_blocker(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "CONTRACT_EVIDENCE_BLOCKER"


def _concrete_blocker(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("verdict") == "CONCRETE_BLOCKER"


def _accepted_count(judge: Any) -> int:
    return len(judge.get("accepted")) if isinstance(judge, dict) and isinstance(judge.get("accepted"), list) else 0


def _feedback(*reports: Any) -> str:
    parts: list[str] = []
    for report in reports:
        if isinstance(report, dict):
            for key in ("findings", "approved_brief", "summary", "rationale", "next_action"):
                value = report.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        elif isinstance(report, str) and report.strip():
            parts.append(report.strip())
    return "\n\n".join(parts) or "No structured feedback."


def _judge_default() -> dict[str, Any]:
    return {
        "verdict": "FAIL",
        "accepted": [],
        "rejected": [],
        "diagnostic": [],
        "validation_brief": "No structured judge output was returned.",
    }


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    tribunal: dict[str, Any],
    candidates: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await ctx.agent(
        BASELINE_JUDGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            tribunal=_dump(tribunal),
            candidates=_dump(candidates),
            cap=cap,
        ),
        schema=BASELINE_JUDGE_SCHEMA,
        label=f"{stage}-validation-judge",
        tools=_read_tools(),
    )
    return _trim(_dict_or(judge, _judge_default()), cap)


def _merge_risks(*risks: Any) -> dict[str, Any]:
    merged: list[dict[str, Any]] = []
    summary: list[str] = []
    evidence: list[str] = []
    seen: set[str] = set()

    for risk in risks:
        block = _dict_or(risk, EMPTY_DIFF_RISKS)
        if isinstance(block, dict):
            summary.append(block.get("summary", "").strip())
            evidence.extend(entry for entry in block.get("evidence", []) if isinstance(entry, str))
            for item in block.get("risks", []):
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id", ""))
                if rid in seen:
                    continue
                seen.add(rid)
                merged.append(item)

    return {
        "risks": merged,
        "summary": "\n\n".join(part for part in summary if part),
        "evidence": evidence,
    }


async def _run_contract_tribunal(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    contracts: dict[str, Any],
    cartography: dict[str, Any],
    inventory: dict[str, Any],
    feedback: str,
) -> dict[str, Any]:
    result = await ctx.agent(
        CONTRACT_TRIBUNAL_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contract_miner=_dump(contracts),
            test_cartographer=_dump(cartography),
            contract_inventory=_dump(inventory),
            feedback=feedback,
        ),
        schema=TRIBUNAL_SCHEMA,
        label="contract-tribunal",
        tools=_read_tools(),
    )
    tribunal = _dict_or(
        result,
        {
            "contracts": [],
            "strong_contract_ids": [],
            "weak_contract_ids": [],
            "hypothesis_contract_ids": [],
            "forbidden_fields": [],
            "rationale": "No structured tribunal output.",
        },
    )
    if not isinstance(tribunal.get("contracts"), list):
        tribunal["contracts"] = []
    if not isinstance(tribunal.get("strong_contract_ids"), list):
        tribunal["strong_contract_ids"] = []
    if not isinstance(tribunal.get("weak_contract_ids"), list):
        tribunal["weak_contract_ids"] = []
    if not isinstance(tribunal.get("hypothesis_contract_ids"), list):
        tribunal["hypothesis_contract_ids"] = []
    if not isinstance(tribunal.get("forbidden_fields"), list):
        tribunal["forbidden_fields"] = []
    return tribunal


async def _run_pre_patch_validation(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    tribunal: dict[str, Any],
    cartography: dict[str, Any],
    label_suffix: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    suffix = f":{label_suffix}" if label_suffix else ""
    judge_stage = f"pre-{label_suffix}" if label_suffix else "pre"
    pre_candidates = await ctx.agent(
        CANDIDATE_GENERATION_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            test_cartography=_dump(cartography),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"pre-patch-validation-factory{suffix}",
        tools=_read_tools(),
    )
    pre_candidates = _dict_or(
        pre_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured pre-patch candidates."},
    )

    pre_judge = await _judge_candidates(
        ctx,
        goal=goal,
        tribunal=tribunal,
        candidates=pre_candidates,
        stage=judge_stage,
        cap=MAX_PRE_TESTS,
    )
    if pre_judge.get("verdict") == "BLOCKED":
        pre_judge["verdict"] = "FAIL"

    baseline_triage = await ctx.agent(
        BASELINE_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(pre_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label=f"baseline-executor-triage{suffix}",
        tools=_tester_tools(),
    )
    baseline_triage = _dict_or(
        baseline_triage,
        {"classifications": [], "approved_brief": "No structured baseline triage.", "abstained": True},
    )
    return pre_candidates, pre_judge, baseline_triage


async def _run_attempt(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    cartography: dict[str, Any],
    tribunal: dict[str, Any],
    pre_judge: dict[str, Any],
    baseline_triage: dict[str, Any],
    inventory: dict[str, Any],
    attempt: int,
    feedback: str,
) -> dict[str, Any]:
    feedback_block = FEEDBACK_BLOCK.format(feedback=feedback) if feedback else ""
    coder_label = "coder:r1" if attempt == 1 else f"coder-minimal-retry:r{attempt}"
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            cartography=_dump(cartography),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            feedback_block=feedback_block,
        ),
        label=coder_label,
        tools=_coder_tools(),
    )

    await ctx.phase(f"existing-tests-approved-validation:r{attempt}")
    etv_report = await ctx.agent(
        ETV_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            coder_report=coder_report or "(coder returned no report)",
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
        ),
        schema=ETV_SCHEMA,
        label=f"existing-tests-approved-validation:r{attempt}",
        tools=_tester_tools(),
    )
    etv_report = _dict_or(
        etv_report,
        {
            "status": "NOT_RUN",
            "findings": "Existing tests and approved validation returned no structured report.",
            "checks": [],
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )

    await ctx.phase(f"diff-risk:r{attempt}")

    def branch_risk_task():
        return ctx.agent(
            DIFF_RISK_BRANCH_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                tribunal=_dump(tribunal),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"branch-boundary-attack:r{attempt}",
            tools=_tester_tools(),
        )

    def regression_risk_task():
        return ctx.agent(
            DIFF_RISK_REGRESSION_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                tribunal=_dump(tribunal),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"regression-scan:r{attempt}",
            tools=_tester_tools(),
        )

    def observable_risk_task():
        return ctx.agent(
            DIFF_RISK_OBSERVABLE_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                inventory=_dump(inventory),
                patch_verdict=_dump(etv_report),
            ),
            schema=DIFF_RISK_SCHEMA,
            label=f"observable-diff-review:r{attempt}",
            tools=_tester_tools(),
        )

    branch_risk, regression_risk, observable_risk = await ctx.parallel(
        [
            branch_risk_task,
            regression_risk_task,
            observable_risk_task,
        ]
    )
    diff_risks = _merge_risks(branch_risk, regression_risk, observable_risk)

    await ctx.phase(f"post-validate:r{attempt}")
    post_candidates = await ctx.agent(
        POST_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            tribunal=_dump(tribunal),
            diff_risks=_dump(diff_risks),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"post-validation-factory:r{attempt}",
        tools=_read_tools(),
    )
    post_candidates = _dict_or(
        post_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured post-patch candidates."},
    )

    await ctx.phase(f"post-validation-judge-triage:r{attempt}")
    post_decision = _trim(
        _dict_or(
            await ctx.agent(
                POST_JUDGE_TRIAGE_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    tribunal=_dump(tribunal),
                    candidates=_dump(post_candidates),
                    etv_report=_dump(etv_report),
                    cap=MAX_POST_TESTS,
                ),
                schema=POST_VALIDATION_DECISION_SCHEMA,
                label=f"post-validation-judge-triage:r{attempt}",
                tools=_tester_tools(),
            ),
            EMPTY_POST_JUDGE,
        ),
        MAX_POST_TESTS,
    )
    if _post_failed_with_evidence(post_decision):
        return {
            "attempt": attempt,
            "status": "coder_minimal_retry",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": EMPTY_SKEPTIC,
            "final_verdict": {
                "verdict": "CONCRETE_BLOCKER",
                "findings": post_decision.get("retry_feedback", "Post validation found an evidence-backed failure."),
                "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
                "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
                "retry_feedback": post_decision.get("retry_feedback", ""),
            },
            "feedback": _feedback(post_decision, etv_report),
            "edge": "VJ2->CR",
            "retry": True,
        }

    await ctx.phase(f"skeptic:r{attempt}")
    skeptic = await ctx.agent(
        FINAL_SKEPTIC_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            etv_report=_dump(etv_report),
            post_decision=_dump(post_decision),
            risks=_dump(diff_risks),
        ),
        schema=SKEPTIC_SCHEMA,
        label=f"final-skeptic:r{attempt}",
        tools=_read_tools(),
    )
    skeptic = _dict_or(
        skeptic,
        {
            "verdict": "PASS",
            "findings": "Final skeptic returned no structured judgement.",
            "required_evidence": [],
            "next_action": "Re-run one minimal retry if verifier still fails.",
        },
    )
    if _contract_evidence_blocker(skeptic):
        return {
            "attempt": attempt,
            "status": "contract_rearbitrate",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": skeptic,
            "final_verdict": {
                "verdict": "CONCRETE_BLOCKER",
                "findings": skeptic.get("findings", "Contract evidence blocker."),
                "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
                "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
                "retry_feedback": skeptic.get("next_action", ""),
            },
            "feedback": _feedback(skeptic, post_decision, etv_report),
            "edge": "FS->CA",
            "retry": True,
        }

    await ctx.phase(f"final-verify:r{attempt}")
    final_verdict = await ctx.agent(
        FINAL_VERIFIER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            tribunal=_dump(tribunal),
            etv_report=_dump(etv_report),
            post_decision=_dump(post_decision),
            risks=_dump(diff_risks),
            skeptic=_dump(skeptic),
        ),
        schema=FINAL_VERIFIER_SCHEMA,
        label=f"final-verifier:r{attempt}",
        tools=_tester_tools(),
    )
    final_verdict = _dict_or(
        final_verdict,
        {
            **EMPTY_FINAL_VERIFIER,
            "allowed_patch_paths": etv_report.get("allowed_patch_paths", []),
            "disallowed_patch_paths": etv_report.get("disallowed_patch_paths", []),
        },
    )

    if _concrete_blocker(final_verdict):
        return {
            "attempt": attempt,
            "status": "coder_minimal_retry",
            "coder_report": coder_report or "",
            "etv_report": etv_report,
            "patch_verdict": etv_report,
            "diff_risks": diff_risks,
            "post_candidates": post_candidates,
            "post_judge": post_decision,
            "post_triage": post_decision,
            "skeptic": skeptic,
            "final_verdict": final_verdict,
            "edge": "FV->CR",
            "feedback": _feedback(skeptic, final_verdict),
            "retry": True,
        }

    return {
        "attempt": attempt,
        "status": "done",
        "coder_report": coder_report or "",
        "etv_report": etv_report,
        "patch_verdict": etv_report,
        "diff_risks": diff_risks,
        "post_candidates": post_candidates,
        "post_judge": post_decision,
        "post_triage": post_decision,
        "skeptic": skeptic,
        "final_verdict": final_verdict,
        "edge": "FV->OUT",
        "feedback": _feedback(skeptic, final_verdict),
        "retry": False,
    }


FEEDBACK_BLOCK = """
Previous attempt findings:
{feedback}"""


@workflow(
    name="swe-committee-v2",
    description="SWE Committee V2 workflow (Analyst, CM, TC, CI, Tribunal, dual judges, risk audit, skeptical gate).",
    phases=[
        "localize",
        "evidence",
        "contract-tribunal",
        "pre-validate",
        "solve",
        "diff-risk",
        "post-validate",
        "skeptic",
        "final-verify",
    ],
)
async def swe_committee_v2(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}

    await ctx.phase("localize")
    localization = await ctx.agent(
        LOCALIZER_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=LOCALIZATION_SCHEMA,
        label="analyst-localizer",
        tools=_read_tools(),
    )
    localization = _dict_or(
        localization,
        {
            "summary": "No structured localization was produced.",
            "root_cause_hypothesis": "",
            "scope_files": [],
            "public_api": [],
            "uncertainties": ["No structured evidence from localization."],
            "definition_of_done": "Fix the issue with a minimal source patch.",
        },
    )

    await ctx.phase("evidence")
    evidence_reports = await ctx.parallel(
        [
            lambda: ctx.agent(
                CONTRACT_MINER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=CONTRACT_MINER_SCHEMA,
                label="contract-miner",
                tools=_read_tools(),
            ),
            lambda: ctx.agent(
                TEST_CARTOGRAPHER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=TEST_CARTOGRAPHY_SCHEMA,
                label="test-cartographer",
                tools=_read_tools(),
            ),
            lambda: ctx.agent(
                CONTRACT_INVENTORY_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=OBSERVABLE_INVENTORY_SCHEMA,
                label="observable-contract-inventory",
                tools=_read_tools(),
            ),
        ]
    )
    contract_miner = _dict_or(
        evidence_reports[0] if evidence_reports else None,
        {"contracts": []},
    )
    cartography = _dict_or(
        evidence_reports[1] if len(evidence_reports) > 1 else None,
        {
            "framework": "",
            "runner_commands": [],
            "test_files": [],
            "fixtures": [],
            "temporary_test_guidance": "No structured cartography was produced.",
        },
    )
    inventory = _dict_or(
        evidence_reports[2] if len(evidence_reports) > 2 else None,
        {
            "observable_fields": [],
            "unaffected_fields": [],
            "risky_fields": [],
            "notes": "No structured inventory was produced.",
        },
    )

    await ctx.phase("contract-tribunal")
    tribunal = await _run_contract_tribunal(
        ctx,
        goal=goal,
        localization=localization,
        contracts=contract_miner,
        cartography=cartography,
        inventory=inventory,
        feedback="",
    )

    await ctx.phase("pre-validate")
    pre_candidates, pre_judge, baseline_triage = await _run_pre_patch_validation(
        ctx,
        goal=goal,
        localization=localization,
        tribunal=tribunal,
        cartography=cartography,
    )

    attempts: list[dict[str, Any]] = []
    feedback = ""
    for attempt in range(1, MAX_CODER_ROUNDS + 1):
        await ctx.phase(f"solve:r{attempt}")
        report = await _run_attempt(
            ctx,
            goal=goal,
            localization=localization,
            cartography=cartography,
            tribunal=tribunal,
            pre_judge=pre_judge,
            baseline_triage=baseline_triage,
            inventory=inventory,
            attempt=attempt,
            feedback=feedback,
        )
        attempts.append(report)

        if report["status"] == "done":
            return {
                "status": "done",
                "rounds": attempt,
                "contracts": len(_dict_or(tribunal, {"contracts": []}).get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }

        feedback = _feedback(report["final_verdict"], report.get("skeptic", {}), report.get("post_judge", {}))
        if report["status"] == "contract_rearbitrate" and attempt < MAX_CODER_ROUNDS:
            await ctx.phase("contract-tribunal")
            tribunal = await _run_contract_tribunal(
                ctx,
                goal=goal,
                localization=localization,
                contracts=contract_miner,
                cartography=cartography,
                inventory=inventory,
                feedback=feedback,
            )
            await ctx.phase("pre-validate")
            pre_candidates, pre_judge, baseline_triage = await _run_pre_patch_validation(
                ctx,
                goal=goal,
                localization=localization,
                tribunal=tribunal,
                cartography=cartography,
                label_suffix=f"rearbitrate-r{attempt}",
            )

        if attempt == MAX_CODER_ROUNDS:
            break

    return {
        "status": "incomplete",
        "rounds": MAX_CODER_ROUNDS,
        "contracts": len(_dict_or(tribunal, {"contracts": []}).get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "post_validation_accepted": _accepted_count(attempts[-1]["post_judge"]) if attempts else 0,
        "allowed_patch_paths": attempts[-1]["final_verdict"].get("allowed_patch_paths", []) if attempts else [],
        "disallowed_patch_paths": attempts[-1]["final_verdict"].get("disallowed_patch_paths", []) if attempts else [],
        "attempts": attempts,
        "tokens_spent": ctx.tokens_spent(),
    }
