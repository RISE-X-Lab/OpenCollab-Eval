"""Execution implementation for :mod:`.validation_council_solve`."""

from __future__ import annotations

from opencollab.workflows import workflow

from ._validation_council_solve_defs import (
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
    Any,
    _accepted_count,
    _candidates_brief,
    _cartography_brief,
    _clip,
    _coder_tools,
    _complete_goal,
    _contracts_brief,
    _dict_or,
    _judge_brief,
    _localization_brief,
    _read_tools,
    _report_brief,
    _source_diff_present,
    _tester_tools,
    _triage_brief,
    _trim_judge,
    coder_role_timeout_seconds,
    structured_role_timeout_seconds,
)


async def _required_agent(ctx: Any, prompt: str, **kwargs: Any) -> Any:
    """Fail the workflow when a role receives no model response at all."""
    tokens_before = ctx.tokens_spent()
    result = await ctx.agent(prompt, **kwargs)
    if (result is None or result == "") and ctx.tokens_spent() <= tokens_before:
        label = str(kwargs.get("label") or "agent")
        raise RuntimeError(f"{label} completed without a successful model response")
    return result


async def _judge_candidates(
    ctx: Any,
    *,
    goal: str,
    contracts: dict[str, Any],
    candidates: dict[str, Any],
    stage: str,
    cap: int,
) -> dict[str, Any]:
    judge = await _required_agent(
        ctx,
        JUDGE_PROMPT.format(
            rules=SHARED_RULES,
            stage=stage,
            cap=cap,
            goal=_complete_goal(goal),
            contracts=_contracts_brief(contracts, 220),
            candidates=_candidates_brief(candidates, cap * 2, 400),
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
    feedback_block = FEEDBACK_BLOCK.format(feedback=_clip(feedback, 220)) if feedback else ""
    failures_before = len(getattr(ctx, "agent_failures", ()))
    coder_report = await _required_agent(
        ctx,
        CODER_PROMPT.format(
            rules=SHARED_RULES,
            goal=_complete_goal(goal),
            localization=_localization_brief(localization, 180),
            contracts=_contracts_brief(contracts, 180),
            cartography=_cartography_brief(cartography),
            pre_judge=_judge_brief(pre_judge, 140),
            baseline_triage=_triage_brief(baseline_triage, 140),
            feedback_block=feedback_block,
        ),
        label=f"coder:r{attempt}",
        tools=_coder_tools(),
        timeout=coder_role_timeout_seconds(),
    )
    source_changed = await _source_diff_present(ctx, injected_test_paths)
    if source_changed is None:
        raise RuntimeError("source diff probe did not return an authoritative result")
    if source_changed is False:
        failures = getattr(ctx, "agent_failures", ())
        if len(failures) > failures_before:
            failure = failures[-1]
            raise RuntimeError(
                "coder session failed before producing a source diff "
                f"({failure.get('exception_type', 'unknown provider error')}, "
                f"status={failure.get('status_code')})"
            )
        return {
            "attempt": attempt,
            "coder_report": coder_report or "",
            "candidate_ready": False,
            "finding": (
                "The coder completed without a source change after injected "
                "validation paths were excluded."
            ),
        }
    return {
        "attempt": attempt,
        "coder_report": coder_report or "",
        "candidate_ready": True,
        "finding": "A source candidate is ready for official evaluation.",
    }


@workflow(
    name="validation-council-solve",
    description="Blind contract-led SWE workflow with one solver and official evaluation",
    phases=["localize", "evidence", "pre-validate", "solve"],
)
async def validation_council_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" or "description"'}
    injected_test_paths = [str(path) for path in args.get("injected_test_paths") or [] if str(path)]

    await ctx.phase("localize")
    localization = await _required_agent(
        ctx,
        LOCALIZER_PROMPT.format(rules=SHARED_RULES, goal=_complete_goal(goal)),
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
    evidence_tokens_before = ctx.tokens_spent()
    evidence_reports = await ctx.parallel(
        [
            lambda: ctx.agent(
                CONTRACT_MINER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=_complete_goal(goal),
                    localization=_localization_brief(localization, 160),
                ),
                schema=CONTRACT_SCHEMA,
                label="contract-miner",
                tools=_read_tools(),
                budget=EVIDENCE_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
            lambda: ctx.agent(
                TEST_CARTOGRAPHER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=_complete_goal(goal),
                    localization=_localization_brief(localization, 160),
                ),
                schema=TEST_CARTOGRAPHY_SCHEMA,
                label="test-cartographer",
                tools=_read_tools(),
                budget=EVIDENCE_BUDGET,
                timeout=structured_role_timeout_seconds(),
            ),
        ]
    )
    if not any(report is not None for report in evidence_reports) and ctx.tokens_spent() <= evidence_tokens_before:
        raise RuntimeError("evidence roles completed without a successful model response")
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
    pre_candidates = await _required_agent(
        ctx,
        PRE_VALIDATION_FACTORY_PROMPT.format(
            rules=SHARED_RULES,
            goal=_complete_goal(goal),
            localization=_localization_brief(localization),
            contracts=_contracts_brief(contracts, 120),
            cartography=_report_brief(_cartography_brief(cartography), 180),
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
        stage="pre",
        cap=MAX_APPROVED_PRE_TESTS,
    )
    if _accepted_count(pre_judge):
        baseline_triage = await _required_agent(
            ctx,
            BASELINE_TRIAGE_PROMPT.format(
                rules=SHARED_RULES,
                goal=_complete_goal(goal),
                judge=_judge_brief(pre_judge, 200),
            ),
            schema=TRIAGE_SCHEMA,
            label="baseline-triage",
            tools=_tester_tools(),
            budget=TRIAGE_BUDGET,
            timeout=structured_role_timeout_seconds(),
        )
        baseline_triage = _dict_or(
            baseline_triage,
            {"classifications": [], "approved_brief": "No baseline triage.", "abstained": True},
        )
    else:
        baseline_triage = {
            "classifications": [],
            "approved_brief": "No accepted baseline probes.",
            "abstained": True,
        }

    baseline_changed = await _source_diff_present(ctx, injected_test_paths)
    if baseline_changed is None:
        raise RuntimeError("source diff probe did not establish a clean pre-coder baseline")
    if baseline_changed:
        raise RuntimeError("pre-coder roles changed the source worktree")

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
        if report["candidate_ready"]:
            return {
                "status": "done",
                "candidate_ready": True,
                "verification": "official_eval_pending",
                "rounds": attempt,
                "contracts": len(contracts.get("contracts", [])),
                "pre_validation_accepted": _accepted_count(pre_judge),
                "attempts": attempts,
                "tokens_spent": ctx.tokens_spent(),
            }
        feedback = report["finding"]
        await ctx.log(f"attempt {attempt} failed: {feedback[:200]}")

    return {
        "status": "incomplete",
        "candidate_ready": False,
        "rounds": MAX_CODER_ROUNDS,
        "contracts": len(contracts.get("contracts", [])),
        "pre_validation_accepted": _accepted_count(pre_judge),
        "attempts": attempts,
        "tokens_spent": ctx.tokens_spent(),
    }
