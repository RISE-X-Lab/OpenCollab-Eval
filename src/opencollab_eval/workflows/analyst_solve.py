"""analyst-solve — analyst-driven reconnaissance, then a phased coder/tester build.

Sibling of ``scout_solve.py`` and ``self_collab.py``. It grafts ``scout_solve``'s
parallel read-only reconnaissance onto ``self_collab``'s phased coder/tester loop,
but the ANALYST stays in charge end to end: it first decomposes the problem into
exploration dimensions, then — after the scouts report — designs the phased fix
itself instead of handing off to a separate synthesizer.

Built for hard tasks where a single shallow pass already failed. Three levers
distinguish it from the siblings:

* it pays for breadth of reconnaissance up front (parallel scouts), so the plan
  starts from a confirmed root cause rather than a guess;
* phases run BEST-EFFORT — a failed phase does not stop the run (it leaves its
  partial edits and the next phase continues), because a partial patch grades
  better than none;
* a budget floor guarantees output: before every expensive step it reserves
  headroom, and if the budget runs low it bails to a single ``forced-write``
  coder whose only job is to land a concrete edit, right or wrong.

Shape:

* analyst (scope) decomposes the PROBLEM into independent exploration dimensions;
* each dimension is investigated in parallel by a read-only scout;
* analyst (plan) synthesizes the findings into a root cause, an approach, and an
  ordered list of implementation phases;
* each phase runs a sequential coder -> tester loop, best-effort;
* a final whole-goal verification gets one repair round if the budget allows.

Select with ``--workflow analyst-solve`` in
``python -m opencollab_eval.generation.gen_prediction_workflow``.

The eval harness runs it unchanged: ``goal`` falls back to the task
``description`` that ``run_eval_task`` passes in its args dict.
"""

from __future__ import annotations

from opencollab.sdk.workflows import workflow

from opencollab_eval.workflows import _analyst_solve_defs as _definitions
from opencollab_eval.workflows import _analyst_solve_runtime as _runtime
from opencollab_eval.workflows._analyst_solve_defs import (
    CODER_PROMPT,
    DIMENSIONS_SCHEMA,
    FINAL_DONE,
    FINDINGS_BLOCK,
    PLAN_BUDGET,
    PLAN_PROMPT,
    PLAN_SCHEMA,
    REPAIR_BUDGET,
    SCOPE_BUDGET,
    SCOPE_PROMPT,
    SHARED_RULES,
    TESTER_BUDGET,
    VERDICT_SCHEMA,
    Any,
    _budget_ok,
    _coder_suffix,
    _coder_tools,
    _f2p_gate,
    _final_verify_redundant,
    _planner_suffix,
    _planner_tools,
    _read_tools,
    _recon_block,
    _target_tests_block,
    _tester_prompt,
    _tester_tools_for,
    _verified_test_targets,
)
from opencollab_eval.workflows._analyst_solve_defs import (
    RECON_FACTS_HEADER as RECON_FACTS_HEADER,
)
from opencollab_eval.workflows._analyst_solve_runtime import (
    _forced_final_write,
    _recon,
    _run_phase,
)

_tester_tools = _definitions._tester_tools


@workflow(
    name="analyst-solve",
    description="Analyst decomposes the problem -> parallel read-only recon -> analyst designs "
    "a phased plan -> best-effort coder/tester loop per phase -> final verify, with a "
    "budget floor that guarantees a patch",
    phases=["scope", "recon", "plan", "implement", "verify"],
)
async def analyst_solve(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # ``goal`` for CLI runs; ``description`` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal" — pass --args \'{"goal": "..."}\''}

    # FAIL_TO_PASS node-ids the run is graded on (threaded by the harness). The
    # block surfaces the BEHAVIOR, never the tests' literal values — see
    # _target_tests_block. Empty for CLI / non-SWE-bench runs. The raw id list
    # drives the code-side hard gate (D2): when present (injection succeeded) a
    # tester PASS must carry proof those node-ids ran green; when empty the gate
    # is bypassed, preserving today's behavior.
    target_tests = _target_tests_block(args)
    fail_to_pass = list(args.get("fail_to_pass") or [])
    # No in-loop test runtime (KOCO) -> tester validates statically instead of
    # running tests. General flag; default False keeps SWE-bench/CLI identical.
    static_verify = bool(args.get("static_verify"))
    # Enforcement wind-down (STEP 0). Default ``off`` keeps every run byte-for-byte
    # identical; ``needs-enforcement`` arms the structural commit brake for
    # budget-myopic models so a read-only scout commits a structured submit at ~80%
    # of its cap instead of being chopped mid-exploration.
    enforcement_strength = str(args.get("enforcement_strength") or "off")
    commit_reserve = int(args.get("commit_reserve") or 25_000)
    # Paths the harness ``git apply``ed (FAIL_TO_PASS test files) but did NOT
    # commit — the tree is dirty with them the whole run. The working-tree gates
    # exclude these so they fire on the agent's SOURCE edit, not the harness's
    # injected tests. Empty for CLI / non-SWE-bench runs (gates == tree_changed).
    injected_test_paths = list(args.get("injected_test_paths") or [])

    # Phase 1 — analyst frames the investigation.
    await ctx.phase("scope")
    scope = await ctx.agent(
        SCOPE_PROMPT.format(rules=SHARED_RULES, goal=goal, target_tests=target_tests),
        schema=DIMENSIONS_SCHEMA,
        label="analyst:scope",
        tools=_read_tools(),
        budget=SCOPE_BUDGET,
    )
    dims = scope.get("dimensions") if isinstance(scope, dict) else None

    # Phase 2 — parallel reconnaissance (skipped gracefully if framing failed).
    await ctx.phase("recon")
    if dims:
        findings_doc = await _recon(ctx, goal, dims, enforcement_strength, commit_reserve)
    else:
        await ctx.log("recon skipped — analyst produced no dimensions")
        findings_doc = "(reconnaissance skipped — proceed from the goal itself)"

    # Phase 3 — analyst designs the phased plan from the findings.
    await ctx.phase("plan")
    plan = await ctx.agent(
        PLAN_PROMPT.format(rules=SHARED_RULES, goal=goal, target_tests=target_tests, findings=findings_doc)
        + _planner_suffix(enforcement_strength),
        schema=PLAN_SCHEMA,
        label="analyst:plan",
        tools=_planner_tools(enforcement_strength),
        budget=PLAN_BUDGET,
    )
    if isinstance(plan, dict) and plan.get("phases"):
        root_cause = plan.get("root_cause", "")
        approach = plan.get("approach", "")
        phases = plan["phases"]
    else:
        # Degrade gracefully: still attempt the fix as one implicit phase rather
        # than abandoning the task with an empty patch.
        await ctx.log("planner produced no usable plan — falling back to a single implicit phase")
        root_cause, approach = "", ""
        phases = [{"goal": goal, "files": [], "done": FINAL_DONE}]

    # Phase 4 — implement phases best-effort; bail to forced write if budget drops.
    await ctx.phase("implement")
    phase_reports: list[dict[str, Any]] = []
    forced = False
    for idx, ph in enumerate(phases):
        report = await _run_phase(
            ctx,
            goal,
            root_cause,
            approach,
            ph,
            idx,
            target_tests,
            fail_to_pass,
            injected_test_paths,
            static_verify,
            enforcement_strength,
            findings_doc,
        )
        phase_reports.append(report)
        if report["status"] in ("budget_low", "empty_tree"):
            progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
            reason = "budget low" if report["status"] == "budget_low" else "empty tree after phase"
            await _forced_final_write(
                ctx,
                goal,
                root_cause,
                approach,
                progress,
                reason=reason,
                injected_test_paths=injected_test_paths,
                enforcement_strength=enforcement_strength,
                recon_findings=findings_doc,
            )
            forced = True
            break
        # Best-effort: a failed/blocked phase does NOT stop the run.
        await ctx.log(f"phase {idx} {report['status']} after {report.get('rounds', 0)} round(s)")

    # P0-2 — forced write on an empty tree, independent of budget. Even when no
    # phase signalled budget_low/empty_tree, if every phase finished but the
    # working tree is still verifiably empty, land a best-effort patch before the
    # final verify rather than reporting "done" with no edit. ``None`` (no probe
    # wired) is treated as "cannot verify" and does NOT trigger a forced write.
    if not forced and (await ctx.source_changed(injected_test_paths)) is False:
        progress = "\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports))
        await _forced_final_write(
            ctx,
            goal,
            root_cause,
            approach,
            progress,
            reason="empty tree after implement",
            injected_test_paths=injected_test_paths,
            enforcement_strength=enforcement_strength,
            recon_findings=findings_doc,
        )
        forced = True

    # Phase 5 — one whole-goal verification, with a single repair round if affordable.
    await ctx.phase("verify")
    final_verdict: dict[str, Any] | None = None
    final_executed_tests: set[str] | None = None
    repaired = False
    # STEP 2B (Phase 2): skip the whole-goal final tester when every phase already
    # passed its own adversarial tester on the current tree and no forced write has
    # touched it since — re-running it would be near-identical checks on the same
    # tree (pure waste). Enforcement-gated, so OFF runs the final tester exactly as
    # the reference. The repair loop stays intact for any failed/blocked phase.
    skip_final_verify = _final_verify_redundant(enforcement_strength, forced, phase_reports)
    if skip_final_verify:
        await ctx.log(
            "verify: skipping redundant final tester — all phases passed and no intervening coder edit (enforcement on)"
        )
    # Run verify EVEN AFTER a forced write: the forced-write coder lands an
    # un-reviewed patch on budget-low, so verifying (and repairing) it is exactly
    # when it matters most. The implement loop reserved RESERVE_TOKENS for this
    # wrap-up and FORCED_WRITE_BUDGET caps the forced write, so a verify slice
    # always survives — hence the light ``reserve=0`` gate (any budget + time).
    if _budget_ok(ctx, 0) and not skip_final_verify:
        final_tester_tools = _tester_tools_for(static_verify)
        final_verdict = await ctx.agent(
            _tester_prompt(static_verify).format(
                rules=SHARED_RULES,
                goal=goal,
                done=FINAL_DONE,
                target_tests=target_tests,
                summary="\n".join(f"- phase {i}: {r['status']}" for i, r in enumerate(phase_reports)),
                root_cause=root_cause,
                approach=approach,
            ),
            schema=VERDICT_SCHEMA,
            label="tester:final",
            tools=final_tester_tools,
            budget=TESTER_BUDGET,
        )
        final_executed_tests = _verified_test_targets(final_tester_tools)
        if isinstance(final_verdict, dict) and final_verdict.get("verdict") == "FAIL" and _budget_ok(ctx, 0):
            await ctx.log("final verify FAILED — one repair round")
            repaired = True
            await ctx.agent(
                CODER_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    root_cause=root_cause,
                    approach=approach,
                    phase_goal="Address the final verification failure across the whole change.",
                    files="(use the tester findings to locate the files)",
                    done=FINAL_DONE,
                    target_tests=target_tests,
                    findings_block=FINDINGS_BLOCK.format(findings=final_verdict.get("findings", "")),
                )
                + _recon_block(findings_doc, enforcement_strength)
                + _coder_suffix(enforcement_strength),
                label="coder:repair",
                tools=_coder_tools(enforcement_strength),
                budget=REPAIR_BUDGET,
            )
            final_tester_tools = _tester_tools_for(static_verify)
            final_verdict = await ctx.agent(
                _tester_prompt(static_verify).format(
                    rules=SHARED_RULES,
                    goal=goal,
                    done=FINAL_DONE,
                    target_tests=target_tests,
                    summary="(post-repair re-check)",
                    root_cause=root_cause,
                    approach=approach,
                ),
                schema=VERDICT_SCHEMA,
                label="tester:final2",
                tools=final_tester_tools,
                budget=TESTER_BUDGET,
            )
            final_executed_tests = _verified_test_targets(final_tester_tools)

    passed_phases = sum(1 for r in phase_reports if r["status"] == "passed")
    # "verified" requires not just a PASS label but the named FAIL_TO_PASS tests
    # green: when ids were injected, the final verdict must also clear the f2p
    # gate, including the current tester tool's GREEN evidence. With no declared
    # ids this collapses to the bare verdict == PASS check.
    verified = (
        isinstance(final_verdict, dict)
        and final_verdict.get("verdict") == "PASS"
        and _f2p_gate(
            final_verdict,
            fail_to_pass,
            executed_tests=final_executed_tests,
        )
        is None
    )
    self_reported_done = verified or (not forced and passed_phases == len(phases) and phases)

    # A run cannot be "done" unless the working tree actually changed in SOURCE
    # (excluding harness-injected tests). The probe answers True/False when wired,
    # or None when it cannot verify. On None we keep the self-reported outcome but
    # flag it as unverified so the caller knows the success was not corroborated by
    # a real diff.
    tree = await ctx.source_changed(injected_test_paths)
    if self_reported_done and tree is False:
        await ctx.log("run marked incomplete — working tree is empty despite a PASS self-report")
        status = "incomplete"
    else:
        status = "done" if self_reported_done else "incomplete"

    result: dict[str, Any] = {
        "status": status,
        "root_cause": root_cause,
        "approach": approach,
        "phases_planned": len(phases),
        "phases_passed": passed_phases,
        "phases": phase_reports,
        "forced_final_write": forced,
        "repaired": repaired,
        "final_verdict": final_verdict,
        # Key name retained for back-compat; its meaning is now SOURCE-scoped
        # (changes outside injected_test_paths), not whole-tree. No external consumer.
        "tree_changed": tree,
        "tokens_spent": ctx.budget.spent(),
    }
    if tree is None:
        result["tree_unverified"] = True
    # STEP 2B: surface the skip for trace auditing. Added ONLY when it fired, so the
    # off-path (and the non-skipped on-path) result shape is unchanged.
    if skip_final_verify:
        result["final_verify_skipped"] = True
    return result


@workflow(
    name="team-pro",
    description="TeamPro dynamic analyst-led reconnaissance and phased coder/tester workflow.",
    phases=["scope", "recon", "plan", "implement", "verify"],
)
async def team_pro(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Stable evaluation-layer name for the tuned analyst-solve workflow."""
    return await analyst_solve(ctx, args)


__all__ = ["analyst_solve", "team_pro"]


_DEFINITION_EXPORTS = tuple(name for name in dir(_definitions) if not name.startswith("_"))


def __getattr__(name: str):
    """Preserve direct imports of legacy definition names."""
    if name in _DEFINITION_EXPORTS:
        return getattr(_definitions, name)
    if not name.startswith("_") and hasattr(_runtime, name):
        return getattr(_runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
