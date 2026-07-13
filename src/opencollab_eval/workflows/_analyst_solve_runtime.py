"""Execution phases for the analyst-driven workflow."""

from __future__ import annotations

from opencollab.sdk.experimental import format_findings_report

from ._analyst_solve_defs import (
    CODER_BUDGET,
    CODER_PROMPT,
    COMMIT_PROMPT,
    DRAFT_PROMPT,
    DRAFT_REVISE_BLOCK,
    ENFORCEMENT_OFF,
    FINAL_DONE,
    FINDINGS_BLOCK,
    FORCED_PROMPT,
    FORCED_WRITE_BUDGET,
    MAX_ROUNDS_PER_PHASE,
    MAX_SCOUTS,
    RECON_FLOOR,
    SCOUT_BUDGET,
    SCOUT_PROMPT,
    SHARED_RULES,
    TESTER_BUDGET,
    VERDICT_SCHEMA,
    Any,
    _budget_ok,
    _coder_suffix,
    _coder_tools,
    _f2p_gate,
    _read_tools,
    _recon_block,
    _tester_prompt,
    _tester_tools_for,
    _time_low,
    _verified_test_targets,
)
from ._fact_sheet import (
    build_fact_sheet,
    estimate_target_complexity,
    format_fact_sheet_hint,
    recon_pool_is_ample,
    size_recon,
)


async def _recon(
    ctx: Any,
    goal: str,
    dims: list[dict[str, Any]],
    enforcement_strength: str = "off",
    commit_reserve: int = 25_000,
) -> str:
    """Fan the dimensions out to parallel read-only scouts; return a combined,
    labelled findings document for the planning analyst.

    ``enforcement_strength`` (default ``off``) threads the STEP-0 wind-down to each
    scout: with ``off`` the scout runs exactly as before; with ``needs-enforcement``
    it gets a submit_findings tool and the structural commit brake (forced to a
    single structured submit at ~80% of its cap instead of being chopped)."""
    if len(dims) > MAX_SCOUTS:
        await ctx.log(f"recon: scope produced {len(dims)} dimensions — capping to {MAX_SCOUTS} scouts")
        dims = dims[:MAX_SCOUTS]

    # STEP 5a/5c (gated on enforcement). OFF -> ``fact_hint`` stays "" and
    # ``depth_leash`` stays 1.0, so the scout count, per-scout cap and hints below
    # are byte-for-byte identical to the reference path.
    fact_hint = ""
    depth_leash = 1.0
    if enforcement_strength != "off":
        # 5a — deterministic, NON-LLM pre-recon fact sheet over the in-workspace
        # (stubbed) source ONLY. Degrade gracefully: a missing workspace root or an
        # un-locatable target yields no manifest, the scouts keep today's hints, and
        # 5c sizing is skipped. The extractor itself refuses to read any answer
        # artifact (test_code/, func_implementation, *_result/_output.jsonl).
        workspace_root = getattr(ctx, "workspace_root", None)
        manifest = None
        if workspace_root:
            try:
                manifest = build_fact_sheet(workspace_root, goal)
            except Exception as exc:  # noqa: BLE001 — recon must never abort on the fact sheet
                await ctx.log(f"recon: fact sheet skipped (extractor error: {exc})")
                manifest = None
        else:
            await ctx.log("recon: fact sheet skipped — no workspace_root on ctx")
        if manifest:
            fact_hint = format_fact_sheet_hint(manifest)
            await ctx.log(
                f"recon: fact sheet built for {manifest['function_name']} "
                f"({manifest['target_file']}): {manifest['call_site_count']} call site(s), "
                f"{len(manifest['siblings'])} sibling(s), "
                f"{len(manifest['referenced_types'])} type ref(s)"
            )
            # 5c — size the scout COUNT + per-scout depth leash from a cheap static
            # complexity estimate, so a trivial target does not get the full fan-out
            # WHEN THE RECON POOL IS THE BINDING CONSTRAINT. With an ample budget
            # (e.g. a 2M run) the pool can fund every scope dimension at the full
            # SCOUT_BUDGET ceiling, so there is nothing to ration: run the full
            # fan-out at full depth and let the in-loop info-gain wind-down /
            # commit-first brake (not a body-blind static proxy) stop a scout that
            # has nothing left to find. This avoids under-reconning a hard target
            # whose static surface reads "simple" (thin/untyped signature,
            # in-workspace-only call sites) — the dominant KOCO failure mode.
            complexity = estimate_target_complexity(manifest)
            n_scouts, sized_leash = size_recon(len(dims), complexity, ceiling=MAX_SCOUTS)
            if recon_pool_is_ample(int(ctx.budget.remaining()), RECON_FLOOR, len(dims), SCOUT_BUDGET):
                depth_leash = 1.0
                await ctx.log(
                    f"recon: complexity={complexity} but pool ample "
                    f"(≥{SCOUT_BUDGET // 1000}k/scout fundable) — keeping full "
                    f"{len(dims)} scout(s) at full depth (5c down-size skipped)"
                )
            elif n_scouts < len(dims):
                depth_leash = sized_leash
                await ctx.log(
                    f"recon: complexity={complexity} -> sizing {len(dims)} dimension(s) "
                    f"down to {n_scouts} scout(s) (depth leash {depth_leash:.2f})"
                )
                dims = dims[:n_scouts]
            else:
                depth_leash = sized_leash
                await ctx.log(
                    f"recon: complexity={complexity} -> keeping {len(dims)} scout(s) (depth leash {depth_leash:.2f})"
                )
        elif workspace_root:
            await ctx.log(
                "recon: fact sheet unavailable (goal names no target / file not found) "
                "— scouts use scope hints unchanged, complexity sizing skipped"
            )

    # Deduct recon from a reserved tail: scouts share only (remaining -
    # RECON_FLOOR), so plan/implement/verify always keep RECON_FLOOR no matter how
    # greedily the read-only scouts explore. min() keeps the SCOUT_BUDGET ceiling
    # binding when the pool is large (e.g. a 2M run).
    n = len(dims)
    recon_pool = max(0, ctx.budget.remaining() - RECON_FLOOR)
    scout_cap = min(SCOUT_BUDGET, recon_pool // n) if n else SCOUT_BUDGET
    # 5c depth leash: shrink each scout's cap for simpler targets so a lone scout
    # cannot just absorb the budget freed by dropping its peers. ``1.0`` (OFF, or
    # the complex bucket) leaves scout_cap untouched.
    if depth_leash < 1.0:
        scout_cap = max(1, int(scout_cap * depth_leash))
    await ctx.log(
        f"recon: {n} scout(s), {ctx.budget.remaining() // 1000}k remaining, "
        f"holding {RECON_FLOOR // 1000}k for plan/implement/verify → "
        f"scout cap {scout_cap // 1000}k each"
    )
    if enforcement_strength != "off":
        # Reserve is carved FROM each scout's cap (explore_threshold =
        # scout_cap - reserve_size), never additive — log it so the wind-down
        # trip point is auditable against submit_turn_cost in the metric.
        await ctx.log(
            f"recon: enforcement={enforcement_strength}, reserve_size={commit_reserve} "
            f"(explore_threshold ~{max(0, scout_cap - commit_reserve) // 1000}k of "
            f"{scout_cap // 1000}k per scout)"
        )

    def _scout_label(d: dict[str, Any], i: int) -> str:
        return f"scout:{i}:{(d.get('aspect') or '').strip().replace(' ', '-')[:24] or 'dim'}"

    def _scout_hints(d: dict[str, Any]) -> str:
        base = "\n".join(d.get("hints") or []) or "(no starting point given — search from the goal)"
        # 5a injection: prepend the static fact sheet so scouts start from confirmed
        # signatures/call-sites instead of re-discovering them. Empty when OFF or no
        # manifest -> returns ``base`` byte-for-byte.
        return f"{fact_hint}\n\n{base}" if fact_hint else base

    # STEP 5b — commit-first (Design B, no FSM changes). Gated on enforcement AND a
    # built fact sheet (``fact_hint`` non-empty) AND a ctx that can run a bounded
    # submit-only draft call. For each scout, commit a turn-0 DRAFT from the static
    # fact sheet (one bounded ``draft_findings`` call) BEFORE it explores; the scout
    # then runs EXACTLY as today (capture→cancel→harvest unchanged), revising the
    # draft into its own refined submit (which is what gets harvested). The draft is
    # also passed as the per-scout HARVEST FALLBACK so a scout that dies/strays before
    # refining never loses the fact-sheet anchors. OFF / no manifest / no draft_findings
    # -> ``draft_texts`` stays all-None and every scout call is byte-for-byte reference.
    draft_texts: list[str | None] = [None] * len(dims)
    draft_fn = getattr(ctx, "draft_findings", None)
    if fact_hint and callable(draft_fn):
        await ctx.log(f"recon: commit-first — drafting {len(dims)} scout(s) from the fact sheet")
        draft_payloads = await ctx.parallel(
            [
                (
                    lambda d=d, i=i: draft_fn(
                        DRAFT_PROMPT.format(
                            aspect=d.get("aspect", f"dimension {i}"),
                            question=d.get("question", ""),
                            fact_hint=fact_hint,
                            hints="\n".join(d.get("hints") or []) or "(no starting point given — search from the goal)",
                        ),
                        label=f"{_scout_label(d, i)}:draft",
                        budget=commit_reserve,
                    )
                )
                for i, d in enumerate(dims)
            ]
        )
        for i, payload in enumerate(draft_payloads):
            if isinstance(payload, dict):
                rendered = format_findings_report(payload)
                if rendered.strip():
                    draft_texts[i] = rendered
        drafted = sum(1 for t in draft_texts if t)
        await ctx.log(f"recon: commit-first — {drafted}/{len(dims)} draft(s) committed")

    def _draft_block(i: int) -> str:
        return DRAFT_REVISE_BLOCK.format(draft=draft_texts[i]) if draft_texts[i] else ""

    reports = await ctx.parallel(
        [
            (
                lambda d=d, i=i: ctx.agent(
                    SCOUT_PROMPT.format(
                        rules=SHARED_RULES,
                        goal=goal,
                        aspect=d.get("aspect", f"dimension {i}"),
                        question=d.get("question", ""),
                        hints=_scout_hints(d),
                        draft_block=_draft_block(i),
                    ),
                    label=_scout_label(d, i),
                    tools=_read_tools(),
                    budget=scout_cap,
                    enforcement_strength=enforcement_strength,
                    commit_reserve=commit_reserve,
                    harvest_fallback=draft_texts[i],
                )
            )
            for i, d in enumerate(dims)
        ]
    )
    usable = sum(1 for r in reports if isinstance(r, str) and r.strip())
    if usable < len(reports):
        await ctx.log(f"recon: {usable}/{len(reports)} scout reports usable")
    sections = []
    for i, (d, rep) in enumerate(zip(dims, reports, strict=True)):
        body = rep if isinstance(rep, str) and rep.strip() else "(scout died — no findings for this dimension)"
        sections.append(f"## Dimension {i}: {d.get('aspect', '')}\nQuestion: {d.get('question', '')}\n\n{body}")
    return "\n\n".join(sections)


async def _run_phase(
    ctx: Any,
    goal: str,
    root_cause: str,
    approach: str,
    ph: dict[str, Any],
    idx: int,
    target_tests: str = "",
    fail_to_pass: list[str] | None = None,
    injected_test_paths: list[str] | None = None,
    static_verify: bool = False,
    enforcement_strength: str = ENFORCEMENT_OFF,
    recon_findings: str = "",
) -> dict[str, Any]:
    """Drive one plan phase through the coder -> tester loop, best-effort.

    Returns a report whose ``status`` is one of: passed, failed, blocked,
    ``budget_low`` — signalling the caller to stop and force a final write while
    the reserve is still intact — or ``empty_tree``: the final round ended with
    the working tree verifiably unchanged (a tester PASS was overridden), so the
    caller should trigger a forced write.
    """
    phase_goal = ph.get("goal", goal)
    files = "\n".join(ph.get("files") or []) or "(analyst did not pin files — keep the change minimal)"
    done = ph.get("done", FINAL_DONE)
    f2p = fail_to_pass or []
    # Source-scope the working-tree gates: the SWE-bench harness ``git apply``s
    # the FAIL_TO_PASS test_patch WITHOUT committing, so the tree is dirty the
    # whole run. Excluding those injected paths makes the gates fire on the
    # AGENT's edit, not the harness's. Empty (CLI / non-SWE-bench) -> behaves as
    # ``tree_changed`` byte-for-byte.
    _inj = injected_test_paths or []
    findings = ""
    rounds = 0
    for round_no in range(1, MAX_ROUNDS_PER_PHASE + 1):
        if not _budget_ok(ctx):
            why = "deadline near" if _time_low(ctx) else "budget below reserve"
            await ctx.log(f"phase {idx}: {why} before round {round_no} — stopping for forced write")
            return {"goal": phase_goal, "status": "budget_low", "rounds": rounds}
        rounds = round_no
        findings_block = FINDINGS_BLOCK.format(findings=findings) if findings else ""
        summary = await ctx.agent(
            CODER_PROMPT.format(
                rules=SHARED_RULES,
                goal=goal,
                root_cause=root_cause,
                approach=approach,
                phase_goal=phase_goal,
                files=files,
                done=done,
                target_tests=target_tests,
                findings_block=findings_block,
            )
            + _recon_block(recon_findings, enforcement_strength)
            + _coder_suffix(enforcement_strength),
            label=f"coder:p{idx}r{round_no}",
            tools=_coder_tools(enforcement_strength),
            budget=CODER_BUDGET,
        )
        # Rung C — early commit (django-11564 step-235 failure mode): a coder that
        # ends having landed NO edit at all this phase (tree still clean) analyzed
        # without committing. Don't spend a tester round verifying nothing —
        # re-issue ONCE with the commit-now forced prompt and a forced tool call,
        # then verify that. Budget-gated (the round top already checked) and
        # bounded to once per round; complements the session-level read-without-
        # write escalation (which can't fire once a coder turn has already
        # stop-ped) and the budget-floor forced write (still the last resort).
        if (await ctx.source_changed(_inj)) is False:
            await ctx.log(f"phase {idx} round {round_no}: coder landed no edit — forcing a commit before testing")
            forced_summary = await ctx.agent(
                COMMIT_PROMPT.format(
                    rules=SHARED_RULES,
                    goal=goal,
                    root_cause=root_cause,
                    approach=approach,
                    progress=f"Round {round_no} coder analyzed but wrote nothing; commit the fix now.",
                )
                + _recon_block(recon_findings, enforcement_strength)
                + _coder_suffix(enforcement_strength),
                label=f"coder:p{idx}r{round_no}-commit",
                tools=_coder_tools(enforcement_strength),
                tool_choice="required",
                budget=CODER_BUDGET,
            )
            if forced_summary is not None:
                summary = forced_summary
        # Disambiguate a dead coder (None) from an empty-output coder (""): the
        # `or` idiom collapsed both, hiding which failure occurred. Pass distinct
        # context to the tester each way.
        if summary is None:
            await ctx.log(f"phase {idx} round {round_no}: coder died (no session result)")
            coder_summary = "(coder died — no session result; verify the working tree yourself)"
        elif not summary.strip():
            await ctx.log(f"phase {idx} round {round_no}: coder produced empty output")
            coder_summary = "(coder produced empty output; verify the working tree yourself)"
        else:
            coder_summary = summary
        tester_tools = _tester_tools_for(static_verify)
        verdict = await ctx.agent(
            _tester_prompt(static_verify).format(
                rules=SHARED_RULES,
                goal=phase_goal,
                done=done,
                target_tests=target_tests,
                summary=coder_summary,
                root_cause=root_cause,
                approach=approach,
            ),
            schema=VERDICT_SCHEMA,
            label=f"tester:p{idx}r{round_no}",
            tools=tester_tools,
            budget=TESTER_BUDGET,
        )
        executed_tests = _verified_test_targets(tester_tools)
        # Diff guard: a tester PASS must NOT stand if the working tree is
        # verifiably unchanged this round — no edit means nothing to pass. Seed
        # the next round so the coder is told it MUST write; on the final round
        # signal the run to force a write.
        tree = await ctx.source_changed(_inj)
        passed = isinstance(verdict, dict) and verdict.get("verdict") == "PASS"
        if passed and tree is False:
            await ctx.log(f"phase {idx} round {round_no}: tester PASS overridden — working tree unchanged")
            findings = (
                "No edit was made this round — the working tree is unchanged. "
                "You MUST call file_write or apply_patch to land a concrete edit."
            )
            await ctx.log(f"phase {idx} round {round_no} FAILED: {findings}")
            if round_no == MAX_ROUNDS_PER_PHASE:
                return {
                    "goal": phase_goal,
                    "status": "empty_tree",
                    "rounds": rounds,
                    "last_findings": findings,
                }
            continue
        # F2P gate (the real lever): a tester PASS must NOT stand unless the run
        # carries proof the named FAIL_TO_PASS tests actually went green —
        # failed_count == 0, every required node-id present in tests_run, and
        # matching GREEN evidence from this call's run_tests instance. Only
        # active when ids were injected (f2p non-empty); empty -> bypass,
        # preserving today's behavior. Mirrors the tree-unchanged override: seed
        # the next round's findings and continue, or fail on the final round.
        if passed:
            gate_findings = _f2p_gate(
                verdict,
                f2p,
                executed_tests=executed_tests,
            )
            if gate_findings is not None:
                await ctx.log(f"phase {idx} round {round_no}: tester PASS overridden — FAIL_TO_PASS proof insufficient")
                findings = gate_findings
                await ctx.log(f"phase {idx} round {round_no} FAILED: {findings[:200]}")
                if round_no == MAX_ROUNDS_PER_PHASE:
                    return {
                        "goal": phase_goal,
                        "status": "failed",
                        "rounds": rounds,
                        "last_findings": findings,
                    }
                continue
        if passed:
            return {"goal": phase_goal, "status": "passed", "rounds": rounds}
        if isinstance(verdict, dict) and verdict.get("verdict") == "BLOCKED":
            blocker = verdict.get("findings", "") or "environmental blocker (unspecified)"
            await ctx.log(f"phase {idx} round {round_no} BLOCKED: {blocker[:200]}")
            return {"goal": phase_goal, "status": "blocked", "rounds": rounds, "blocker": blocker}
        if verdict is None:
            await ctx.log(
                f"phase {idx} round {round_no} tester subagent DIED "
                "(no verdict — agent error/timeout/budget) — substituting generic findings"
            )
        elif not isinstance(verdict, dict):
            await ctx.log(
                f"phase {idx} round {round_no} tester returned an UNEXPECTED type "
                f"({type(verdict).__name__}) — substituting generic findings"
            )
        # Never re-issue an identical task: the next round carries the findings.
        findings = (
            verdict.get("findings", "") if isinstance(verdict, dict) else ""
        ) or "Tester returned no verdict. Re-verify the definition of done yourself before reporting."
        await ctx.log(f"phase {idx} round {round_no} FAILED: {findings[:200]}")
    return {"goal": phase_goal, "status": "failed", "rounds": rounds, "last_findings": findings}


def _seconds_left(ctx: Any) -> float:
    """Wall-clock seconds left before the hard deadline; ``inf`` when unbounded.

    Defensive: a ctx without ``seconds_left`` (unbounded CLI runs, older test
    stubs) reports ``inf`` so no timeout is imposed where no deadline is wired.
    """
    seconds_left = getattr(ctx, "seconds_left", None)
    return float(seconds_left()) if callable(seconds_left) else float("inf")


async def _forced_final_write(
    ctx: Any,
    goal: str,
    root_cause: str,
    approach: str,
    progress: str,
    *,
    reason: str,
    injected_test_paths: list[str] | None = None,
    enforcement_strength: str = ENFORCEMENT_OFF,
    recon_findings: str = "",
) -> str:
    """Spend the reserved headroom on one coder that MUST land an edit.

    ``reason`` distinguishes the trigger in the log ("budget low" vs "empty tree
    after implement"). The coder runs with ``tool_choice="required"`` so the
    provider forces a tool call — the session layer falls back to "auto" once if
    the endpoint rejects "required".

    This is the last action of the run and its whole job is to GUARANTEE a patch
    lands before the hard wall, so it is hardened three ways:
    ``over_budget_ok=True`` skips ``WorkflowContext.agent``'s pre-call budget raise
    so the write still runs after the meter hits zero — without it the forced write
    self-aborted on an exhausted budget and no coder round ran at all (sympy-11400);
    ``thinking=False`` forces reasoning off so the generation is fast and cannot
    blow the deadline margin even when the run-wide default is thinking-on
    (analyst-solve eval runs with OPENCOLLAB_THINKING=1); and ``timeout`` clamps
    the call to whatever wall-clock time is left, so a stalled call is cancelled
    inside the workflow — its on-disk edits survive — instead of being truncated
    by the outer wall (which lost django-11564).
    """
    await ctx.log(f"forced write: {reason} — landing a best-effort patch")
    result = await ctx.agent(
        FORCED_PROMPT.format(
            rules=SHARED_RULES,
            goal=goal,
            root_cause=root_cause,
            approach=approach,
            progress=progress or "(no prior coder edits recorded)",
        )
        + _recon_block(recon_findings, enforcement_strength)
        + _coder_suffix(enforcement_strength),
        label="coder:forced-write",
        tools=_coder_tools(enforcement_strength),
        tool_choice="required",
        thinking=False,
        timeout=_seconds_left(ctx),
        over_budget_ok=True,
        budget=FORCED_WRITE_BUDGET,
    )
    # Post-attempt outcome so the trajectory distinguishes a patch that LANDED from
    # one that ABORTED (coder died / timed out / budget). Prefer the SOURCE probe —
    # ground truth that an edit reached disk OUTSIDE the harness-injected tests —
    # and fall back to the coder's return value when no probe is wired (CLI / older
    # stubs report None).
    probe = getattr(ctx, "source_changed", None)
    changed = await probe(injected_test_paths or []) if callable(probe) else None
    if changed is True:
        await ctx.log(f"forced write: {reason} — LANDED a patch (working tree changed)")
    elif changed is False:
        await ctx.log(f"forced write: {reason} — ABORTED: no edit reached disk")
    elif result is not None:
        await ctx.log(f"forced write: {reason} — coder returned (tree change unverified)")
    else:
        await ctx.log(f"forced write: {reason} — ABORTED: coder died before writing")
    return result
