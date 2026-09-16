"""Candidate-workspace implementation of the lean validation council."""

from __future__ import annotations

import hashlib
from typing import Any

from opencollab_eval.patch_diff import (
    is_eval_test_path,
    normalize_patch_path,
    patch_entries,
    split_patch_blocks,
)
from opencollab_eval.patch_paths import is_generated_runtime_artifact_path

from ._validation_council_lean_official_critic import (
    RISK_CRITIC_BUDGET,
    RISK_CRITIC_PROMPT,
    RISK_CRITIC_SCHEMA,
)
from ._validation_council_lean_official_critic import (
    normalize_risk_critic as _normalize_risk_critic,
)
from ._validation_council_lean_official_defs import (
    BASELINE_TRIAGE_PROMPT,
    CANDIDATE_TESTS_SCHEMA,
    CODER_PROMPT,
    CONTRACT_MINER_PROMPT,
    CONTRACT_SCHEMA,
    EVIDENCE_BUDGET,
    FEEDBACK_BLOCK,
    JUDGE_BUDGET,
    JUDGE_PROMPT,
    JUDGE_SCHEMA,
    LOCALIZATION_SCHEMA,
    LOCALIZER_BUDGET,
    LOCALIZER_PROMPT,
    MAX_APPROVED_PRE_TESTS,
    MAX_CODER_ROUNDS,
    PRE_VALIDATION_FACTORY_PROMPT,
    SHARED_RULES,
    TEST_CARTOGRAPHER_PROMPT,
    TEST_CARTOGRAPHY_SCHEMA,
    TRIAGE_BUDGET,
    TRIAGE_SCHEMA,
    VALIDATION_FACTORY_BUDGET,
    _accepted_count,
    _candidates_brief,
    _cartographer_tools,
    _cartography_brief,
    _coder_tools,
    _complete_goal,
    _contracts_brief,
    _dict_or,
    _dump,
    _is_blocked,
    _is_pass,
    _judge_brief,
    _localization_brief,
    _read_tools,
    _report_brief,
    _risk_tools,
    _source_diff_present,
    _tester_tools,
    _triage_brief,
    _trim_judge,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)
from ._validation_council_lean_official_post_patch import (
    INTEGRATED_VERIFIER_BUDGET,
    INTEGRATED_VERIFIER_PROMPT,
    POST_PATCH_EVIDENCE_SCHEMA,
    VERDICT_SERIALIZER_BUDGET,
    VERDICT_SERIALIZER_PROMPT,
    _apply_risk_critic,
    _critic_evidence_brief,
    _enforce_post_patch_gates,
    _evidence_text,
    _has_actionable_retry,
    _normalize_post_patch_evidence,
    _post_patch_check_count,
    _retry_feedback,
)


def _shared_rules(ctx: Any) -> str:
    workspace_root = getattr(ctx, "workspace_root", None) or "."
    return SHARED_RULES.format(workspace_root=workspace_root)


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    contracts: dict[str, Any],
    candidates: dict[str, Any],
    cartography: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await ctx.agent(
        JUDGE_PROMPT.format(
            rules=_shared_rules(ctx),
            stage=stage,
            cap=cap,
            goal=_complete_goal(goal),
            contracts=_contracts_brief(contracts, 6_000),
            candidates=_candidates_brief(candidates, cap, 400),
            cartography=_cartography_brief(cartography),
        ),
        schema=JUDGE_SCHEMA,
        label=f"{stage}-validation-judge",
        tools=_read_tools(),
        budget=JUDGE_BUDGET,
        timeout=structured_role_timeout_seconds(),
    )
    return _trim_judge(
        _dict_or(
            judge,
            {
                "accepted": [],
                "rejected": [],
                "diagnostic": [],
                "validation_brief": "Judge returned no structured decision.",
            },
        ),
        cap,
    )


def _normalize_baseline_triage(value: Any) -> dict[str, Any]:
    report = _dict_or(
        value,
        {"classifications": [], "approved_brief": "No baseline triage.", "abstained": True},
    )
    normalized: list[dict[str, Any]] = []
    for raw in report.get("classifications", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        status = str(item.get("status") or "not_run")
        command = str(item.get("command") or "").strip()
        observed = bool(item.get("observed"))
        if status in {
            "base_fail_repro",
            "base_pass_regression",
            "base_environment_failure",
            "patch_pass",
            "patch_fail",
        } and (
            not observed or not command
        ):
            item["status"] = "not_run"
            item["observed"] = False
            item["evidence"] = "Unexecuted or commandless claim was downgraded to not_run."
        if item.get("status") == "base_environment_failure" and not str(
            item.get("failure_signature") or ""
        ).strip():
            item["status"] = "not_run"
            item["observed"] = False
            item["evidence"] = "Environment failure without a signature was downgraded to not_run."
        normalized.append(item)
    return {**report, "classifications": normalized}


async def _patch_facts(ctx: Any, injected_test_paths: list[str]) -> dict[str, Any]:
    """Derive changed/protected paths from the actual diff, not model prose."""
    diff_fn = getattr(ctx, "diff", None)
    raw_diff = await diff_fn() if callable(diff_fn) else None
    if not isinstance(raw_diff, str):
        return {
            "changed_files": None,
            "actual_disallowed_changed_files": [],
            "protected_paths": ["existing public tests", "harness-injected tests", "generated artifacts"],
            "diff_sha256": None,
            "diff_excerpt": "(working-tree diff unavailable)",
        }

    injected = {normalize_patch_path(path) for path in injected_test_paths if path}
    changed: dict[str, None] = {}
    disallowed: dict[str, None] = {}
    for old_path, new_path in patch_entries(raw_diff):
        old_normalized = normalize_patch_path(old_path)
        new_normalized = normalize_patch_path(new_path)
        endpoints = [path for path in (old_normalized, new_normalized) if path and path not in injected]
        is_new_test = not old_normalized and bool(new_normalized) and is_eval_test_path(new_normalized)
        if is_new_test:
            continue
        for path in endpoints:
            changed.setdefault(path, None)
            if is_generated_runtime_artifact_path(path):
                disallowed.setdefault(path, None)
        if old_normalized and is_eval_test_path(old_normalized) and old_normalized not in injected:
            for path in endpoints:
                disallowed.setdefault(path, None)

    patch_body = "".join(
        "".join(block)
        for block in split_patch_blocks(raw_diff)
        if block and block[0].startswith("diff --git ")
    )
    return {
        "changed_files": list(changed),
        "actual_disallowed_changed_files": list(disallowed),
        "protected_paths": ["existing public tests", "harness-injected tests", "generated artifacts"],
        "diff_sha256": hashlib.sha256(
            (patch_body or raw_diff).encode("utf-8", errors="surrogatepass")
        ).hexdigest(),
        "diff_excerpt": raw_diff[:16_000],
    }


async def _run_verifier(
    ctx: Any,
    *,
    prompt: str,
    label: str,
    goal: str,
    contracts: dict[str, Any],
    coder_report: Any,
    injected_test_paths: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one verifier in the isolated candidate workspace."""
    before_facts = await _patch_facts(ctx, injected_test_paths)
    prompt = prompt.replace("{diff_facts}", _evidence_text(before_facts, 16_000))
    raw_evidence: Any = await ctx.agent(
        prompt,
        schema=POST_PATCH_EVIDENCE_SCHEMA,
        label=label,
        tools=_tester_tools(),
        budget=INTEGRATED_VERIFIER_BUDGET,
        timeout=structured_role_timeout_seconds(),
    )
    after_facts = await _patch_facts(ctx, injected_test_paths)
    before_hash = before_facts.get("diff_sha256")
    after_hash = after_facts.get("diff_sha256")
    unchanged = before_hash is None or after_hash is None or before_hash == after_hash

    if not isinstance(raw_evidence, dict):
        raw_evidence = await ctx.agent(
            VERDICT_SERIALIZER_PROMPT.format(
                rules=_shared_rules(ctx),
                goal=_complete_goal(goal),
                contracts=_contracts_brief(contracts, 6_000),
                diff_facts=_evidence_text(before_facts, 16_000),
                tool_ledger="(No public runtime ledger is available; use only the supplied artifacts.)",
                coder_report=_report_brief(coder_report or "(coder returned no report)", 800),
            ),
            schema=POST_PATCH_EVIDENCE_SCHEMA,
            label=f"{label}:serializer",
            tools=[],
            budget=VERDICT_SERIALIZER_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )

    evidence = _normalize_post_patch_evidence(raw_evidence)
    if unchanged is False:
        evidence = {
            **evidence,
            "verdict": "BLOCKED",
            "findings": "Verifier changed the isolated candidate source diff; its verdict is not trustworthy.",
            "remaining_defects": ["Verifier changed the isolated candidate source diff."],
        }
    return evidence, before_facts


async def _run_critic(
    ctx: Any,
    *,
    prompt: str,
    label: str,
    injected_test_paths: list[str],
) -> dict[str, Any]:
    """Run the critic and reject evidence if it changes the candidate diff."""
    before_facts = await _patch_facts(ctx, injected_test_paths)
    raw_report: Any = await ctx.agent(
        prompt,
        schema=RISK_CRITIC_SCHEMA,
        label=label,
        tools=_risk_tools(),
        budget=RISK_CRITIC_BUDGET,
        timeout=structured_role_timeout_seconds(),
    )
    after_facts = await _patch_facts(ctx, injected_test_paths)
    before_hash = before_facts.get("diff_sha256")
    after_hash = after_facts.get("diff_sha256")
    unchanged = before_hash is None or after_hash is None or before_hash == after_hash
    report = _normalize_risk_critic(raw_report)
    if unchanged is False:
        return {
            "recommendation": "INCONCLUSIVE",
            "risks": [],
            "summary": "Risk critic altered the candidate source diff; its evidence was discarded.",
        }
    return report


async def _run_attempt(
    ctx: Any,
    *,
    goal: str,
    localization: dict[str, Any],
    contracts: dict[str, Any],
    cartography: dict[str, Any],
    pre_judge: dict[str, Any],
    baseline_triage: dict[str, Any],
    attempt: int,
    feedback: str,
    injected_test_paths: list[str],
) -> dict[str, Any]:
    feedback_block = FEEDBACK_BLOCK.format(feedback=feedback) if feedback else ""
    coder_report = await ctx.agent(
        CODER_PROMPT.format(
            rules=_shared_rules(ctx),
            goal=_complete_goal(goal),
            localization=_localization_brief(localization, 180),
            contracts=_dump(contracts),
            cartography=_cartography_brief(cartography),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            feedback_block=feedback_block,
        ),
        label=f"coder:r{attempt}",
        tools=_coder_tools(),
        timeout=coder_role_timeout_seconds(),
    )

    await ctx.phase(f"verify:r{attempt}")
    initial_evidence, patch_facts = await _run_verifier(
        ctx,
        prompt=INTEGRATED_VERIFIER_PROMPT.format(
            rules=_shared_rules(ctx),
            goal=_complete_goal(goal),
            contracts=_contracts_brief(contracts, 6_000),
            pre_judge=_judge_brief(pre_judge, 8_000),
            baseline_triage=_triage_brief(baseline_triage, 12_000),
            cartography=_cartography_brief(cartography),
            coder_report=_report_brief(coder_report or "(coder returned no report)", 800),
            diff_facts="{diff_facts}",
            previous_evidence="(none; inspect and execute the verification plan now)",
            critic_report="(none; a concise critic runs only if this verifier is uncertain)",
            mode="Initial verification. NEEDS_CRITIC is available for genuine uncertainty.",
        ),
        label=f"integrated-verifier:r{attempt}",
        goal=goal,
        contracts=contracts,
        coder_report=coder_report,
        injected_test_paths=injected_test_paths,
    )
    source_changed = await _source_diff_present(ctx, injected_test_paths)
    initial_evidence = _enforce_post_patch_gates(
        initial_evidence,
        source_changed=source_changed,
        critic_may_be_requested=True,
        actual_changed_files=patch_facts.get("changed_files"),
        actual_disallowed_changed_files=patch_facts.get("actual_disallowed_changed_files"),
        protected_paths=patch_facts.get("protected_paths"),
        patch_fingerprint=patch_facts.get("diff_sha256"),
        baseline_triage=baseline_triage,
    )

    critic_report: dict[str, Any] | None = None
    final_evidence = initial_evidence
    if initial_evidence.get("verdict") == "NEEDS_CRITIC":
        await ctx.phase(f"critic:r{attempt}")
        critic_report = await _run_critic(
            ctx,
            label=f"risk-critic:r{attempt}",
            injected_test_paths=injected_test_paths,
            prompt=RISK_CRITIC_PROMPT.format(
                rules=_shared_rules(ctx),
                goal=_complete_goal(goal),
                contracts=_contracts_brief(contracts, 6_000),
                post_patch_evidence=_critic_evidence_brief(initial_evidence),
                diff_facts=_evidence_text(patch_facts, 16_000),
            ),
        )
        final_evidence = _apply_risk_critic(initial_evidence, critic_report)
        if critic_report.get("recommendation") == "PASS":
            final_evidence = _enforce_post_patch_gates(
                final_evidence,
                source_changed=source_changed,
                critic_may_be_requested=False,
                actual_changed_files=patch_facts.get("changed_files"),
                actual_disallowed_changed_files=patch_facts.get("actual_disallowed_changed_files"),
                protected_paths=patch_facts.get("protected_paths"),
                patch_fingerprint=patch_facts.get("diff_sha256"),
                baseline_triage=baseline_triage,
                critic_confirmed=True,
            )

    return {
        "attempt": attempt,
        "coder_report": coder_report or "",
        "initial_post_patch_evidence": initial_evidence,
        "risk_critic": critic_report,
        "post_patch_evidence": final_evidence,
        "final_verdict": final_evidence,
        "patch_fingerprint": patch_facts.get("diff_sha256"),
        "actionable_retry": _has_actionable_retry(final_evidence),
    }


async def run_validation_council_lean_candidate(
    ctx: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    injected_test_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]

    await ctx.phase("localize")
    localization = await ctx.agent(
        LOCALIZER_PROMPT.format(rules=_shared_rules(ctx), goal=_complete_goal(goal)),
        schema=LOCALIZATION_SCHEMA,
        label="analyst-localizer",
        tools=_read_tools(),
        budget=LOCALIZER_BUDGET,
        timeout=structured_role_timeout_seconds(),
    )
    localization = _dict_or(
        localization,
        {
            "summary": "No structured localization was produced.",
            "root_cause_hypothesis": "",
            "files": [],
            "public_api": [],
            "uncertainties": ["localizer returned no structured output"],
            "definition_of_done": "Resolve the issue with a minimal source patch.",
        },
    )

    await ctx.phase("evidence")
    evidence_reports = await ctx.parallel(
        [
            lambda: ctx.agent(
                CONTRACT_MINER_PROMPT.format(
                    rules=_shared_rules(ctx),
                    goal=_complete_goal(goal),
                    localization=_localization_brief(localization, 180),
                ),
                schema=CONTRACT_SCHEMA,
                label="contract-miner",
                tools=_read_tools(),
                budget=EVIDENCE_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
            lambda: ctx.agent(
                TEST_CARTOGRAPHER_PROMPT.format(
                    rules=_shared_rules(ctx),
                    goal=_complete_goal(goal),
                    localization=_localization_brief(localization, 180),
                ),
                schema=TEST_CARTOGRAPHY_SCHEMA,
                label="test-cartographer",
                tools=_cartographer_tools(),
                budget=EVIDENCE_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
        ]
    )
    contracts = _dict_or(
        evidence_reports[0] if evidence_reports else None,
        {"contracts": [], "coverage_matrix": []},
    )
    cartography = _dict_or(
        evidence_reports[1] if len(evidence_reports) > 1 else None,
        {
            "framework": "",
            "working_runner_commands": [],
            "runner_commands": [],
            "test_files": [],
            "fixtures": [],
            "assertion_style": "",
            "available_tools": [],
            "unavailable_tools": [],
            "available_dependencies": [],
            "unavailable_dependencies": [],
            "capabilities": [],
            "diagnostics": [],
            "temporary_test_guidance": "No structured cartography was produced.",
        },
    )

    await ctx.phase("pre-validate")
    pre_candidates = await ctx.agent(
        PRE_VALIDATION_FACTORY_PROMPT.format(
            rules=_shared_rules(ctx),
            goal=_complete_goal(goal),
            localization=_localization_brief(localization, 180),
            contracts=_contracts_brief(contracts, 6_000),
            cartography=_cartography_brief(cartography),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label="pre-validation-factory",
        tools=_read_tools(),
        budget=VALIDATION_FACTORY_BUDGET,
        timeout=structured_role_timeout_seconds(),
    )
    pre_candidates = _dict_or(
        pre_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured pre-patch candidates."},
    )
    pre_judge = await _judge_candidates(
        ctx,
        goal=goal,
        contracts=contracts,
        candidates=pre_candidates,
        cartography=cartography,
        stage="pre",
        cap=MAX_APPROVED_PRE_TESTS,
    )
    if _accepted_count(pre_judge):
        baseline_triage = await ctx.agent(
            BASELINE_TRIAGE_PROMPT.format(
                rules=_shared_rules(ctx),
                goal=_complete_goal(goal),
                judge=_judge_brief(pre_judge, 200),
                cartography=_cartography_brief(cartography),
            ),
            schema=TRIAGE_SCHEMA,
            label="baseline-triage",
            tools=_tester_tools(),
            budget=TRIAGE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
        baseline_triage = _normalize_baseline_triage(baseline_triage)
    else:
        baseline_triage = {
            "classifications": [],
            "approved_brief": "No accepted baseline probes.",
            "abstained": True,
        }

    attempts: list[dict[str, Any]] = []
    feedback = ""
    for attempt in range(1, MAX_CODER_ROUNDS + 1):
        await ctx.phase(f"solve:r{attempt}")
        report = await _run_attempt(
            ctx,
            goal=goal,
            localization=localization,
            contracts=contracts,
            cartography=cartography,
            pre_judge=pre_judge,
            baseline_triage=baseline_triage,
            attempt=attempt,
            feedback=feedback,
            injected_test_paths=injected_test_paths,
        )
        attempts.append(report)
        if _is_pass(report["final_verdict"]):
            return {
                "status": "done",
                "rounds": attempt,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_patch_checks": _post_patch_check_count(report["post_patch_evidence"]),
                "critic_invocations": sum(item["risk_critic"] is not None for item in attempts),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }
        blocked = _is_blocked(report["final_verdict"])
        if blocked and not report.get("actionable_retry"):
            blocker = report["final_verdict"].get("findings", "")
            await ctx.log(f"attempt {attempt} blocked: {blocker}")
            return {
                "status": "blocked",
                "rounds": attempt,
                "blocker": blocker,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_patch_checks": _post_patch_check_count(report["post_patch_evidence"]),
                "critic_invocations": sum(item["risk_critic"] is not None for item in attempts),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }
        if blocked:
            await ctx.log(
                f"attempt {attempt} reported BLOCKED but supplied actionable retry evidence; "
                "routing the evidence to the next Coder round"
            )
        next_feedback = _retry_feedback(report["post_patch_evidence"])
        await ctx.log(f"attempt {attempt} failed: {next_feedback}")
        if not report.get("actionable_retry"):
            await ctx.log(
                f"attempt {attempt} produced no concrete failed command, contract, or source location; "
                "stopping instead of asking the Coder to guess"
            )
            break
        if (
            attempt > 1
            and report.get("patch_fingerprint") is not None
            and report.get("patch_fingerprint") == attempts[-2].get("patch_fingerprint")
            and next_feedback == feedback
        ):
            await ctx.log(
                f"attempt {attempt} repeated the same patch and verification feedback; stopping no-op retry loop"
            )
            break
        feedback = next_feedback

    return {
        "status": "incomplete",
        "rounds": len(attempts),
        "contracts": len(contracts.get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "post_patch_checks": _post_patch_check_count(attempts[-1]["post_patch_evidence"]) if attempts else 0,
        "critic_invocations": sum(item["risk_critic"] is not None for item in attempts),
        "allowed_patch_paths": attempts[-1]["final_verdict"].get("allowed_patch_paths", []) if attempts else [],
        "disallowed_patch_paths": attempts[-1]["final_verdict"].get("disallowed_patch_paths", []) if attempts else [],
        "attempts": attempts,
        "tokens_spent": ctx.tokens_spent(),
    }


__all__ = ["run_validation_council_lean_candidate"]
