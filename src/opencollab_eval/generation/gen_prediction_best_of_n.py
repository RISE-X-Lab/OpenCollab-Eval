"""Generate a SWE-bench prediction with the Best-of-N arm.

N non-communicating candidates, each given ``1/N`` of one seat's budget, and a
fixed selector that submits one of them. The arm differs from the single-agent
arm in how compute is allocated and in nothing else: same task text, same
system prompt, same tools, same container image, same trusted host extraction.
Candidates do not talk to each other, do not share a working tree, and do not
see each other's work -- so this is not a collaboration arm, and its place in
the comparison is to separate "more compute, spent in parallel tries" from
"more agents, organised".

**Each candidate gets its own container.** The alternative -- N sessions inside
one workflow -- cannot give a per-candidate diff, because extraction reads one
workspace against one baseline; there would be nothing for a selector to
select. It also runs into a concurrency ceiling that is applied silently. One
container per candidate costs N container starts and buys N independently
extracted, independently verified patches.

Run it directly (the OpenCollab venv, absolute paths)::

    python -m opencollab_eval.generation.gen_prediction_best_of_n \\
        --instance-file /path/to/instance_sympy-20590.json \\
        --output /path/to/predictions-best-of-n.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from opencollab_eval.engine.async_runtime import run_with_bounded_shutdown
from opencollab_eval.runtime_config import resolve_runtime_config as get_config
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS, model_context_window

from .best_of_n_selector import Candidate, Selection, select_candidate
from .container_quiescence import container_image_id, require_container_quiescence
from .gen_prediction_agent import build_task, load_instance, run_agent
from .gen_prediction_config import (
    bind_llm_transport,
    default_container_image,
    llm_transport_metrics,
    unique_container_name,
    validate_generation_limits,
)
from .gen_prediction_constants import DEFAULT_BUDGET, DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT
from .gen_prediction_docker import (
    finalize_container_ownership,
    start_container_with_marker,
)
from .gen_prediction_patch import extract_patch_guarded, prepare_trusted_patch_baseline
from .gen_prediction_run_summary import RUN_SUMMARY_KEY, build_run_summary
from .gen_prediction_safe_output import (
    append_output_records,
    build_output_records,
    complete_single_agent_integrity,
    metrics_have_completed_identity,
    normalize_trusted_extraction_status,
    output_paths,
    persist_generation_failure,
)
from .gen_prediction_snapshot import prepare_solver_git_snapshot

#: How many candidates one run of this arm opens.
#:
#: Frozen by preregistration at three. Read off this constant by the batch
#: driver and by the cross-arm alignment audit rather than written down in
#: either of them, so the number this arm runs at and the number the registry
#: declares cannot come apart.
CANDIDATES = 3

#: The name this arm's rows carry in the predictions file.
WORKFLOW_NAME = "best-of-n"


def candidate_run_directory(run_dir: Path, instance_id: str, index: int) -> Path:
    """Where one candidate's container ownership and artifacts live.

    Its own directory, and this is not tidiness. Container ownership is
    recorded partly in flat files -- ``container.id`` and ``container.name`` --
    directly in the run directory. N candidates sharing one directory would
    each overwrite the previous candidate's markers: after the last one, the
    markers name one container and the others are named by nothing, so a run
    that dies mid-flight leaks every container but the last and the recovery
    pass cannot find them.

    Stable across attempts rather than unique to one, because
    ``start_container_with_marker`` runs the recovery pass over this directory
    before it creates anything: a retry of the same (instance, candidate) is
    what cleans up what a crashed earlier attempt left running.
    """
    return run_dir / ".opencollab" / WORKFLOW_NAME / instance_id / f"candidate-{index}"


async def run_candidate(
    *,
    task: str,
    cid: str,
    cfg: dict,
    max_steps: int,
    budget: int,
    timeout: float,
    candidates: int,
    artifact_root: Path,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Run one candidate: the single arm's own agent, in its own container.

    Both of this arm's allocations are applied here and nowhere else, and they
    are divided for different reasons.

    ``budget`` is the arm's whole pool -- the same figure the single-agent arm
    is given -- and each candidate gets ``1/N`` of it. That division *is* the
    arm: N tries on one seat's budget, not N seats.

    ``timeout`` is divided too, which the token pool's reasoning does not by
    itself require. It is divided because ``--timeout`` bounds one whole run on
    every other arm: N candidates each given the flag in full would hand this
    arm N times the wall clock of the arm it is compared against, on a resource
    no arm is supposed to have more of.

    ``max_steps`` is *not* divided. It is already a per-session ceiling on every
    arm -- a team of three seats gets three times the flag as well -- so
    dividing it here would make this the one arm whose sessions are held to a
    different ceiling. The consequence, that this arm's run-level step ceiling
    is N times the single arm's, is declared as a defect in the arm registry
    rather than fixed here, because fixing it changes what a run does.
    """
    return await run_agent(
        task,
        cid,
        cfg,
        max_steps=max_steps,
        budget=budget // candidates,
        timeout=timeout / candidates,
        artifact_root=artifact_root,
        runtime=runtime,
    )


def _candidate_metrics(metrics: dict, cfg: dict, *, budget: int, max_steps: int) -> None:
    """Add the settings a candidate ran under, in the keys every arm uses."""
    metrics.update(
        {
            "llm_model": cfg["model"],
            "llm_provider": cfg["provider"],
            "context_window": model_context_window(cfg["model"]),
            "temperature": cfg.get("temperature"),
            "top_p": cfg.get("top_p"),
            "max_output_tokens": cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            "budget": budget,
            "max_steps": max_steps,
            **llm_transport_metrics(cfg),
        }
    )
    bind_llm_transport(metrics)


def solve_candidate(
    *,
    instance: dict,
    image: str,
    cfg: dict,
    index: int,
    candidates: int,
    max_steps: int,
    budget: int,
    timeout: float,
    run_dir: Path,
    keep_container: bool,
) -> Candidate:
    """Start one container, run one candidate in it, and extract its patch.

    A candidate that fails is recorded as a failed candidate and does not end
    the run: with N containers per run the chance of one Docker or provider
    fault is N times a single-agent run's, and throwing away the two candidates
    that worked would cost far more than it protects. It is not silent -- the
    failure is written into that candidate's own metrics, the run reports how
    many candidates completed, and a candidate with no patch is not eligible
    for selection.

    Container *cleanup* failure is different and is left to propagate: a leaked
    container on a shared machine is somebody else's problem, and the ownership
    marker deliberately survives to say so.
    """
    iid = instance["instance_id"]
    name = unique_container_name("oc-bon-", iid)
    run_dir.mkdir(parents=True, exist_ok=True)
    cid = start_container_with_marker(image, name, run_dir)
    print(f"  candidate {index}: container {cid}")
    patch = ""
    tree: str | None = None
    metrics: dict[str, Any] = {}
    trusted_baseline = None
    try:
        generation_image_id = container_image_id(cid)
        snapshot = prepare_solver_git_snapshot(cid, str(instance.get("base_commit") or ""))
        trusted_baseline = prepare_trusted_patch_baseline(cid, snapshot)
        task = build_task(instance)
        metrics = run_with_bounded_shutdown(
            run_candidate(
                task=task,
                cid=cid,
                cfg=cfg,
                max_steps=max_steps,
                budget=budget,
                timeout=timeout,
                candidates=candidates,
                artifact_root=run_dir,
            )
        )
        _candidate_metrics(metrics, cfg, budget=budget // candidates, max_steps=max_steps)
        metrics["generation_image_id"] = generation_image_id
        metrics["solver_git_snapshot"] = snapshot.as_dict()
        if metrics.get("candidate_probe_eligible") is True:
            require_container_quiescence(cid)
            metrics["container_execution_quiesced"] = True
            metrics["execution_quiesced"] = metrics.get("session_quiesced") is True
            metrics["submission_eligible"] = metrics["execution_quiesced"] is True
            patch, removed_artifacts, extraction = extract_patch_guarded(
                cid, trusted_baseline
            )
            metrics["trusted_patch_extraction"] = extraction
            metrics["removed_generated_artifacts"] = removed_artifacts
            tree = extraction.get("candidate_tree")
            extraction_succeeded = True
            normalize_trusted_extraction_status(metrics, patch)
        else:
            extraction_succeeded = False
            metrics["container_execution_quiesced"] = False
            metrics["execution_quiesced"] = False
            metrics["submission_eligible"] = False
        complete_single_agent_integrity(
            metrics,
            patch=patch,
            patch_extraction_succeeded=extraction_succeeded,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        persist_generation_failure(
            run_dir,
            instance_id=iid,
            phase=f"{WORKFLOW_NAME}_candidate_{index}",
            error=exc,
        )
        patch, tree = "", None
        metrics = {
            **metrics,
            "workflow_status": "error",
            "execution_quiesced": False,
            "submission_eligible": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(f"  candidate {index}: failed with {type(exc).__name__}: {exc}")
    finally:
        if trusted_baseline is not None:
            trusted_baseline.cleanup()
        finalize_container_ownership(
            run_dir=run_dir,
            cid=cid,
            name=name,
            keep_container=keep_container,
            completed=metrics_have_completed_identity(metrics, patch),
            metrics=metrics,
        )
    metrics["patch_produced"] = bool(patch.strip())
    metrics["submitted_patch_chars"] = len(patch)
    metrics["candidate_index"] = index
    metrics["candidate_tree"] = tree
    _write_candidate_record(run_dir, index=index, patch=patch, metrics=metrics)
    return Candidate(index=index, patch=patch, tree=tree, metrics=metrics)


def _write_candidate_record(
    run_dir: Path, *, index: int, patch: str, metrics: dict
) -> None:
    """Keep each candidate on disk as it is produced.

    This arm cannot use the pending-output staging the other arms use: that
    staging holds a finished prediction against a container that is still
    alive, and this arm does not know which patch it is submitting until every
    container has been finalised. So the durability it can offer is this --
    a run that dies after candidate two still shows what candidates zero and
    one produced, instead of leaving only a missing row.
    """
    payload = {"candidate_index": index, "patch": patch, "metrics": metrics}
    (run_dir / "candidate.json").write_text(
        json.dumps(payload, default=str), encoding="utf-8"
    )


def _candidate_summary(candidate: Candidate) -> dict[str, Any]:
    metrics = candidate.metrics
    summary = metrics.get(RUN_SUMMARY_KEY) or {}
    return {
        "index": candidate.index,
        "patch_chars": len(candidate.patch),
        "candidate_tree": candidate.tree,
        "workflow_status": metrics.get("workflow_status"),
        "used_tokens": metrics.get("used_tokens"),
        "step_count": metrics.get("step_count"),
        "duration_s": summary.get("duration_s"),
        "trajectory_path": metrics.get("trajectory_path"),
        "error": metrics.get("error"),
    }


def _total(candidates: list[Candidate], key: str) -> int:
    return sum(int(candidate.metrics.get(key) or 0) for candidate in candidates)


def combine_metrics(
    candidates: list[Candidate],
    selection: Selection,
    *,
    declared_candidates: int,
    pool: int,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """The one metric record this run writes, and the patch it submits.

    Built on the selected candidate's own record, because the proofs a
    prediction is checked against -- the trusted extraction, the tree, the
    integrity flags -- are proofs about *that* patch and about no other.

    Three run-level quantities are then overwritten with sums across the
    candidates: ``run_summary`` (the block every arm is compared on, whose
    ``steps`` and ``tokens`` are documented as run totals -- on a team, the sum
    over the agents that ran), ``used_tokens`` and ``step_count``. What this arm
    spends is what all N candidates spent; reporting only the winner's spend
    would make it look like the cheapest arm in the comparison by discarding
    two thirds of its bill. The per-candidate figures stay, under
    ``best_of_n.candidates``.
    """
    selected = next(
        (candidate for candidate in candidates if candidate.index == selection.index),
        None,
    )
    source = selected if selected is not None else candidates[0]
    patch = selected.patch if selected is not None else ""
    metrics = dict(source.metrics)
    complete_single_agent_integrity(
        metrics,
        patch=patch,
        patch_extraction_succeeded=bool(patch.strip() and source.tree),
    )
    normalize_trusted_extraction_status(metrics, patch)
    metrics["patch_produced"] = bool(patch.strip())
    metrics["submitted_patch_chars"] = len(patch)

    summary = dict(metrics.get(RUN_SUMMARY_KEY) or {})
    steps_total = _total(candidates, "step_count")
    tokens_total = _total(candidates, "used_tokens")
    metrics[RUN_SUMMARY_KEY] = build_run_summary(
        steps=steps_total,
        tokens=tokens_total,
        status=summary.get("status"),
        reason=summary.get("reason"),
        duration_s=sum(
            float((candidate.metrics.get(RUN_SUMMARY_KEY) or {}).get("duration_s") or 0.0)
            for candidate in candidates
        ),
        error=summary.get("error"),
    )
    metrics["step_count"] = steps_total
    metrics["used_tokens"] = tokens_total
    metrics["budget"] = pool
    metrics["best_of_n"] = {
        **selection.as_dict(),
        "candidates_declared": declared_candidates,
        "candidates_completed": sum(
            1 for candidate in candidates if not candidate.metrics.get("error")
        ),
        "candidate_budget": pool // declared_candidates,
        "candidate_timeout_s": timeout / declared_candidates,
        "tokens_total": tokens_total,
        "steps_total": steps_total,
        "candidates": [_candidate_summary(candidate) for candidate in candidates],
    }
    return patch, metrics


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Generate one SWE-bench prediction with the Best-of-N arm"
    )
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument(
        "--metrics",
        default=None,
        help="Metrics JSONL to append to (default: metrics.jsonl beside --output)",
    )
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--top-p", type=float)
    ap.add_argument("--max-output-tokens", type=int)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help="The whole pool for the run; each candidate is given 1/N of it",
    )
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument(
        "--candidates",
        type=int,
        default=CANDIDATES,
        help=(
            "How many candidates to run. The experiment's N is frozen at "
            f"{CANDIDATES} by preregistration and the batch driver never passes "
            "this; it exists so a smoke test can run one candidate. The value "
            "used is written into every run's metrics."
        ),
    )
    ap.add_argument("--keep-container", action="store_true")
    return ap


def main() -> None:
    ap = _parser()
    args = ap.parse_args()
    try:
        args.max_steps, args.budget, args.timeout = validate_generation_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
        )
    except ValueError as exc:
        ap.error(str(exc))
    if isinstance(args.candidates, bool) or args.candidates < 1:
        ap.error("--candidates must be a positive integer")
    if args.budget // args.candidates < 1:
        ap.error("--budget leaves each candidate no tokens")
    out_path, metrics_path = output_paths(args.output, args.metrics)

    instance = load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or default_container_image(args.arch, iid)

    cfg = get_config(str(Path.cwd()))
    if args.model:
        cfg["model"] = args.model
    if args.provider:
        cfg["provider"] = args.provider
    if args.temperature is not None:
        if not 0.0 <= args.temperature <= 2.0:
            ap.error("--temperature must be between 0 and 2")
        cfg["temperature"] = args.temperature
    if args.top_p is not None:
        if not 0.0 <= args.top_p <= 1.0:
            ap.error("--top-p must be between 0 and 1")
        cfg["top_p"] = args.top_p
    if args.max_output_tokens is not None:
        if args.max_output_tokens <= 0:
            ap.error("--max-output-tokens must be positive")
        cfg["max_output_tokens"] = args.max_output_tokens
    model_name = args.model_name or f"opencollab-{WORKFLOW_NAME}-{cfg['model']}"

    print(f"Instance:   {iid}")
    print(f"Image:      {image}")
    print(f"Model:      {cfg['model']} (provider={cfg['provider']})")
    print(
        f"Candidates: {args.candidates} x "
        f"{args.budget // args.candidates} tokens, "
        f"{args.timeout / args.candidates:.0f}s each"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out_path.parent
    # Sequential, because a task container is heavy and the machine a batch
    # runs on is shared: how many containers to have up at once is a decision
    # about somebody else's machine, and the batch driver already owns it.
    # Candidates are independent by construction, so running them one after
    # another changes nothing about what each one does.
    candidates = [
        solve_candidate(
            instance=instance,
            image=image,
            cfg=cfg,
            index=index,
            candidates=args.candidates,
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
            run_dir=candidate_run_directory(run_dir, iid, index),
            keep_container=args.keep_container,
        )
        for index in range(args.candidates)
    ]

    selection = select_candidate(candidates)
    patch, metrics = combine_metrics(
        candidates,
        selection,
        declared_candidates=args.candidates,
        pool=args.budget,
        timeout=args.timeout,
    )
    print(
        f"Selected:   candidate {selection.index} by {selection.rule}"
        f"{' (arbitrary)' if selection.arbitrary else ''}"
    )

    record, metric_record = build_output_records(
        instance_id=iid,
        model_name=model_name,
        patch=patch,
        metrics=metrics,
        workflow_name=WORKFLOW_NAME,
    )
    append_output_records(out_path, metrics_path, record, metric_record)

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (no candidate had anything to submit)")

    if not metrics_have_completed_identity(metric_record, patch):
        raise SystemExit(1)


__all__ = [
    "CANDIDATES",
    "WORKFLOW_NAME",
    "candidate_run_directory",
    "combine_metrics",
    "main",
    "run_candidate",
    "solve_candidate",
]


if __name__ == "__main__":
    main()
