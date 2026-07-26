"""Execution implementation for :mod:`.validation_council_solve`."""

from __future__ import annotations

from opencollab.workflows import workflow

from ._validation_council_solve_defs import (
    BASELINE_TRIAGE_PROMPT,
    CANDIDATE_TESTS_SCHEMA,
    CODER_PROMPT,
    CODER_ROLE_TIMEOUT_SECONDS,
    CONTRACT_MINER_PROMPT,
    CONTRACT_SCHEMA,
    DIFF_RISK_PROMPT,
    DIFF_RISK_SCHEMA,
    EMPTY_DIFF_RISKS,
    EMPTY_POST_CANDIDATES,
    EMPTY_POST_JUDGE,
    EMPTY_POST_TRIAGE,
    EVIDENCE_BUDGET,
    FEEDBACK_BLOCK,
    FINAL_VERIFIER_PROMPT,
    JUDGE_BUDGET,
    JUDGE_PROMPT,
    JUDGE_SCHEMA,
    LOCALIZATION_SCHEMA,
    LOCALIZER_BUDGET,
    LOCALIZER_PROMPT,
    MAX_APPROVED_POST_TESTS,
    MAX_APPROVED_PRE_TESTS,
    MAX_CODER_ROUNDS,
    PATCH_VALIDATOR_PROMPT,
    POST_TRIAGE_PROMPT,
    POST_VALIDATION_FACTORY_PROMPT,
    PRE_VALIDATION_FACTORY_PROMPT,
    RISK_BUDGET,
    SHARED_RULES,
    STRUCTURED_ROLE_TIMEOUT_SECONDS,
    TEST_CARTOGRAPHER_PROMPT,
    TEST_CARTOGRAPHY_SCHEMA,
    TRIAGE_BUDGET,
    TRIAGE_SCHEMA,
    VALIDATION_FACTORY_BUDGET,
    VERDICT_SCHEMA,
    VERIFIER_BUDGET,
    Any,
    _accepted_count,
    _coder_tools,
    _dict_or,
    _dump,
    _feedback,
    _is_blocked,
    _is_pass,
    _read_tools,
    _risk_tools,
    _source_diff_present,
    _tester_tools,
    _trim_judge,
)


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    contracts: dict[str, Any],
    candidates: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await ctx.agent(
        JUDGE_PROMPT.format(
            rules=SHARED_RULES,
            stage=stage,
            cap=cap,
            goal=goal,
            contracts=_dump(contracts),
            candidates=_dump(candidates),
        ),
        schema=JUDGE_SCHEMA,
        label=f"{stage}-validation-judge",
        tools=_read_tools(),
        budget=JUDGE_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
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
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            cartography=_dump(cartography),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            feedback_block=feedback_block,
        ),
        label=f"coder:r{attempt}",
        tools=_coder_tools(),
        timeout=CODER_ROLE_TIMEOUT_SECONDS,
    )
    patch_verdict = await ctx.agent(
        PATCH_VALIDATOR_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            coder_report=coder_report or "(coder returned no report)",
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
        ),
        schema=VERDICT_SCHEMA,
        label=f"patch-validator:r{attempt}",
        tools=_tester_tools(),
        budget=VERIFIER_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    patch_verdict = _dict_or(
        patch_verdict,
        {
            "verdict": "FAIL",
            "findings": "Patch validator returned no structured verdict.",
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )
    if _is_blocked(patch_verdict):
        return {
            "attempt": attempt,
            "coder_report": coder_report or "",
            "patch_verdict": patch_verdict,
            "diff_risks": EMPTY_DIFF_RISKS,
            "post_candidates": EMPTY_POST_CANDIDATES,
            "post_judge": EMPTY_POST_JUDGE,
            "post_triage": EMPTY_POST_TRIAGE,
            "final_verdict": patch_verdict,
        }

    await ctx.phase(f"diff-risk:r{attempt}")
    risks = await ctx.agent(
        DIFF_RISK_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            contracts=_dump(contracts),
            patch_verdict=_dump(patch_verdict),
        ),
        schema=DIFF_RISK_SCHEMA,
        label=f"diff-risk-auditor:r{attempt}",
        tools=_risk_tools(),
        budget=RISK_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    risks = _dict_or(risks, {"risks": [], "summary": "Diff risk auditor returned no structured report."})

    post_candidates = await ctx.agent(
        POST_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            contracts=_dump(contracts),
            risks=_dump(risks),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label=f"post-validation-factory:r{attempt}",
        tools=_read_tools(),
        budget=VALIDATION_FACTORY_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    post_candidates = _dict_or(
        post_candidates,
        {"tests": [], "abstained": True, "rationale": "No structured post-patch candidates."},
    )

    post_judge = await _judge_candidates(
        ctx,
        goal=goal,
        contracts=contracts,
        candidates=post_candidates,
        stage=f"post-r{attempt}",
        cap=MAX_APPROVED_POST_TESTS,
    )

    post_triage = await ctx.agent(
        POST_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(post_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label=f"post-validation-triage:r{attempt}",
        tools=_tester_tools(),
        budget=TRIAGE_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    post_triage = _dict_or(
        post_triage,
        {"classifications": [], "approved_brief": "No post-patch triage.", "abstained": True},
    )

    await ctx.phase(f"final-verify:r{attempt}")
    final_verdict = await ctx.agent(
        FINAL_VERIFIER_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            pre_judge=_dump(pre_judge),
            baseline_triage=_dump(baseline_triage),
            coder_report=coder_report or "(coder returned no report)",
            patch_verdict=_dump(patch_verdict),
            risks=_dump(risks),
            post_judge=_dump(post_judge),
            post_triage=_dump(post_triage),
        ),
        schema=VERDICT_SCHEMA,
        label=f"final-verifier:r{attempt}",
        tools=_tester_tools(),
        budget=VERIFIER_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    final_verdict = _dict_or(
        final_verdict,
        {
            "verdict": "FAIL",
            "findings": "Final verifier returned no structured verdict.",
            "allowed_patch_paths": [],
            "disallowed_patch_paths": [],
        },
    )
    source_changed = await _source_diff_present(ctx, injected_test_paths)
    if source_changed is False:
        final_verdict = {
            **final_verdict,
            "verdict": "FAIL",
            "findings": (
                "Executable diff guard failed: git status reports no tracked "
                "source changes after excluding injected validation files. " + str(final_verdict.get("findings") or "")
            ),
            "allowed_patch_paths": [],
        }

    return {
        "attempt": attempt,
        "coder_report": coder_report or "",
        "patch_verdict": patch_verdict,
        "diff_risks": risks,
        "post_candidates": post_candidates,
        "post_judge": post_judge,
        "post_triage": post_triage,
        "final_verdict": final_verdict,
    }


@workflow(
    name="validation-council-solve",
    description="Blind contract-led SWE workflow with validation judges, diff risk audit, and capped retry",
    phases=["localize", "evidence", "pre-validate", "solve", "diff-risk", "final-verify"],
)
async def validation_council_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    injected_test_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]

    await ctx.phase("localize")
    localization = await ctx.agent(
        LOCALIZER_PROMPT.format(rules=SHARED_RULES, goal=goal),
        schema=LOCALIZATION_SCHEMA,
        label="analyst-localizer",
        tools=_read_tools(),
        budget=LOCALIZER_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
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
                    rules=SHARED_RULES,
                    goal=goal,
                    localization=_dump(localization),
                ),
                schema=CONTRACT_SCHEMA,
                label="contract-miner",
                tools=_read_tools(),
                budget=EVIDENCE_BUDGET,
                timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
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
                budget=EVIDENCE_BUDGET,
                timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
            ),
        ]
    )
    contracts = _dict_or(evidence_reports[0] if evidence_reports else None, {"contracts": []})
    cartography = _dict_or(
        evidence_reports[1] if len(evidence_reports) > 1 else None,
        {
            "framework": "",
            "runner_commands": [],
            "test_files": [],
            "fixtures": [],
            "assertion_style": "",
            "temporary_test_guidance": "No structured cartography was produced.",
        },
    )

    await ctx.phase("pre-validate")
    pre_candidates = await ctx.agent(
        PRE_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            localization=_dump(localization),
            contracts=_dump(contracts),
            cartography=_dump(cartography),
        ),
        schema=CANDIDATE_TESTS_SCHEMA,
        label="pre-validation-factory",
        tools=_read_tools(),
        budget=VALIDATION_FACTORY_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
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
        stage="pre",
        cap=MAX_APPROVED_PRE_TESTS,
    )
    baseline_triage = await ctx.agent(
        BASELINE_TRIAGE_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            judge=_dump(pre_judge),
        ),
        schema=TRIAGE_SCHEMA,
        label="baseline-triage",
        tools=_tester_tools(),
        budget=TRIAGE_BUDGET,
        timeout=STRUCTURED_ROLE_TIMEOUT_SECONDS,
    )
    baseline_triage = _dict_or(
        baseline_triage,
        {"classifications": [], "approved_brief": "No baseline triage.", "abstained": True},
    )

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
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }
        if _is_blocked(report["final_verdict"]):
            blocker = report["final_verdict"].get("findings", "")
            await ctx.log(f"attempt {attempt} blocked: {blocker[:200]}")
            return {
                "status": "blocked",
                "rounds": attempt,
                "blocker": blocker,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "post_validation_accepted": _accepted_count(report["post_judge"]),
                "allowed_patch_paths": report["final_verdict"].get("allowed_patch_paths", []),
                "disallowed_patch_paths": report["final_verdict"].get("disallowed_patch_paths", []),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }
        feedback = _feedback(
            report["final_verdict"],
            report["patch_verdict"],
            report["post_triage"],
            report["diff_risks"],
        )
        await ctx.log(f"attempt {attempt} failed: {feedback[:200]}")

    return {
        "status": "incomplete",
        "rounds": MAX_CODER_ROUNDS,
        "contracts": len(contracts.get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "post_validation_accepted": _accepted_count(attempts[-1]["post_judge"]) if attempts else 0,
        "allowed_patch_paths": attempts[-1]["final_verdict"].get("allowed_patch_paths", []) if attempts else [],
        "disallowed_patch_paths": attempts[-1]["final_verdict"].get("disallowed_patch_paths", []) if attempts else [],
        "attempts": attempts,
        "tokens_spent": ctx.tokens_spent(),
    }
