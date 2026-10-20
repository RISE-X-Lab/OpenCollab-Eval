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
import time
from itertools import count
from pathlib import Path
from typing import Any

from opencollab.profiles import resolve_profile_name

from opencollab_eval.candidate_bytes import MAX_CANDIDATE_FILE_BYTES
from opencollab_eval.engine.async_runtime import add_exception_note, run_with_bounded_shutdown
from opencollab_eval.engine.swe_eval_records import open_regular_binary
from opencollab_eval.runtime_config import resolve_runtime_config as get_config
from opencollab_eval.safe_files import write_regular_bytes_atomic
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS, model_context_window

from .best_of_n_retention import retain_best_of_n_source
from .best_of_n_selector import Candidate, Selection, select_candidate
from .container_quiescence import (
    container_image_id,
    isolate_container_for_preservation,
    require_container_quiescence,
)
from .gen_prediction_agent import build_task, load_instance, resolve_agent_generation_limits, run_agent
from .gen_prediction_config import (
    apply_model_overrides,
    bind_llm_transport,
    default_container_image,
    llm_transport_metrics,
    unique_container_name,
    validate_generation_limits,
)
from .gen_prediction_constants import DEFAULT_BUDGET, DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT
from .gen_prediction_docker import (
    container_owner_path,
    finalize_container_ownership,
    mark_container_kept,
    mark_container_preservation_required,
    prepare_testbed_environment,
    start_container_with_marker,
)
from .gen_prediction_patch import extract_patch_guarded, prepare_trusted_patch_baseline
from .gen_prediction_pending import recover_generation_state
from .gen_prediction_run_summary import RUN_SUMMARY_KEY, build_run_summary
from .gen_prediction_runtime import (
    remove_solver_runtime_dependencies,
    restore_solver_runtime_dependencies,
    stash_solver_runtime_dependencies,
)
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

#: Preregistered candidate count shared by the generator and batch audit.
CANDIDATES = 3

WORKFLOW_NAME = "best-of-n"


def candidate_run_directory(run_dir: Path, instance_id: str, index: int) -> Path:
    """Return the stable ownership and artifact directory for one candidate.

    Separate directories prevent candidates from overwriting container markers.
    Reuse the directory across attempts so startup can recover a crashed attempt.
    """
    return run_dir / ".opencollab" / WORKFLOW_NAME / instance_id / f"candidate-{index}"


async def run_candidate(
    *,
    task: str,
    cid: str,
    cfg: dict,
    max_steps: int | None,
    budget: int | None,
    timeout: float,
    candidates: int,
    artifact_root: Path,
    runtime: Any | None = None,
    profile: str = "base",
) -> dict[str, Any]:
    """Run one candidate with the single-agent solver in its own container.

    Divide the arm's token pool and total timeout across candidates. Keep
    max_steps as the common per-session ceiling, giving this arm N times the
    single-agent run's step ceiling as recorded in the arm registry.
    """
    return await run_agent(
        task,
        cid,
        cfg,
        max_steps=max_steps,
        budget=None if budget is None else budget // candidates,
        timeout=timeout / candidates,
        artifact_root=artifact_root,
        runtime=runtime,
        profile=profile,
    )


def _candidate_metrics(metrics: dict, cfg: dict, *, budget: int, max_steps: int) -> None:
    """Add the settings a candidate ran under, in the keys every arm uses."""
    metrics.update(
        {
            "llm_model": cfg["model"],
            "llm_provider": cfg["provider"],
            "context_window": cfg.get("context_window") or model_context_window(cfg["model"]),
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
    max_steps: int | None,
    budget: int | None,
    timeout: float,
    run_dir: Path,
    keep_container: bool,
    profile: str = "base",
) -> Candidate:
    """Run one candidate in its own container and extract its patch.

    Record generation failures per candidate and continue with the others.
    Candidates without patches are ineligible for selection. Cleanup failures
    propagate and retain the ownership marker for recovery.
    """
    iid = instance["instance_id"]
    name = unique_container_name("oc-bon-", iid)
    run_dir.mkdir(parents=True, exist_ok=True)
    _preserve_previous_candidate(run_dir)
    cid = None
    started = time.monotonic()
    patch = ""
    tree: str | None = None
    metrics: dict[str, Any] = {}
    trusted_baseline = None
    generation_image_id = None
    generation_error: BaseException | None = None
    extraction_succeeded = False
    try:
        cid = start_container_with_marker(image, name, run_dir)
        # The owner must survive a later full-disk candidate write failure.
        # Arm preservation before the model can create a patch.
        mark_container_preservation_required(run_dir, cid)
        print(f"  candidate {index}: container {cid}")
        generation_image_id = container_image_id(cid)
        prepare_testbed_environment(cid)
        solver_runtime = stash_solver_runtime_dependencies(cid, str(instance.get("base_commit") or ""))
        snapshot = prepare_solver_git_snapshot(cid, str(instance.get("base_commit") or ""))
        trusted_baseline = prepare_trusted_patch_baseline(cid, snapshot)
        restore_solver_runtime_dependencies(cid, solver_runtime)
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
                profile=profile,
            )
        )
        _candidate_metrics(metrics, cfg, budget=None if budget is None else budget // candidates, max_steps=max_steps)
        metrics["generation_image_id"] = generation_image_id
        metrics["solver_git_snapshot"] = snapshot.as_dict()
        if metrics.get("candidate_probe_eligible") is True:
            require_container_quiescence(cid)
            metrics["container_execution_quiesced"] = True
            metrics["execution_quiesced"] = metrics.get("session_quiesced") is True
            metrics["submission_eligible"] = metrics["execution_quiesced"] is True and metrics.get(
                "workflow_status"
            ) in {"done", "done_with_timeout_patch"}
            remove_solver_runtime_dependencies(cid, solver_runtime)
            patch, removed_artifacts, extraction = extract_patch_guarded(cid, trusted_baseline)
            extraction_succeeded = True
            metrics["trusted_patch_extraction"] = extraction
            metrics["removed_generated_artifacts"] = removed_artifacts
            tree = extraction.get("candidate_tree")
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
        if trusted_baseline is not None:
            generation_error = exc
        _record_candidate_failure(
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
            RUN_SUMMARY_KEY: build_run_summary(
                steps=metrics.get("step_count"),
                tokens=metrics.get("used_tokens"),
                status="failed",
                reason=type(exc).__name__,
                duration_s=time.monotonic() - started,
                error=exc,
            ),
        }
        print(f"  candidate {index}: failed with {type(exc).__name__}: {exc}")
        owner_path = container_owner_path(run_dir, name)
        if cid is None and (owner_path.exists() or owner_path.is_symlink()):
            raise RuntimeError(
                "candidate startup retained unresolved container ownership; "
                "inspect candidate.json and the owned container before retrying"
            ) from exc
    except BaseException as exc:
        generation_error = exc
        metrics.update(
            workflow_status="interrupted",
            execution_quiesced=False,
            submission_eligible=False,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _record_candidate_failure(
            run_dir,
            instance_id=iid,
            phase=f"{WORKFLOW_NAME}_candidate_{index}_interrupted",
            error=exc,
        )
        raise
    finally:
        # The extracted patch must reach durable storage while its source still
        # exists. A failed write keeps the owned container for recovery.
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        metrics["candidate_index"] = index
        metrics["candidate_tree"] = tree
        retention_required = cid is not None and (
            generation_error is not None
            or trusted_baseline is not None and metrics.get("patch_extraction_succeeded") is not True
        )

        def retain_source(error: BaseException | None, reason: str) -> None:
            retain_best_of_n_source(
                run_dir=run_dir,
                instance=instance,
                image=image,
                index=index,
                cid=cid,
                name=name,
                baseline=trusted_baseline,
                generation_image_id=generation_image_id,
                metrics=metrics,
                error=error,
                reason=reason,
                mark_kept=mark_container_kept,
                quiesce=require_container_quiescence,
                isolate=isolate_container_for_preservation,
                persist_failure=persist_generation_failure,
            )

        if retention_required:
            retain_source(
                generation_error,
                "candidate_record_unwritten" if generation_error is not None and extraction_succeeded
                else "trusted_patch_extraction_incomplete",
            )
        try:
            _write_candidate_record(run_dir, index=index, patch=patch, metrics=metrics)
        except BaseException as write_error:
            if retention_required:
                if generation_error is not None:
                    add_exception_note(
                        generation_error,
                        f"candidate record could not be written: {type(write_error).__name__}: {write_error}",
                    )
                _record_candidate_failure(
                    run_dir,
                    instance_id=iid,
                    phase=f"{WORKFLOW_NAME}_candidate_{index}_persistence",
                    error=write_error,
                )
                if generation_error is None:
                    raise
            else:
                retain_source(write_error, "candidate_record_unwritten")
                raise
        if not retention_required:
            try:
                try:
                    if trusted_baseline is not None:
                        trusted_baseline.cleanup()
                finally:
                    if cid is not None:
                        finalize_container_ownership(
                            run_dir=run_dir,
                            cid=cid,
                            name=name,
                            keep_container=keep_container,
                            completed=metrics_have_completed_identity(metrics, patch),
                            metrics=metrics,
                        )
            except BaseException as cleanup_error:
                _record_candidate_failure(
                    run_dir,
                    instance_id=iid,
                    phase=f"{WORKFLOW_NAME}_candidate_{index}_cleanup",
                    error=cleanup_error,
                )
                raise
            # The first record already protects the patch if this optional update
            # fails after the container has been retired.
            try:
                _write_candidate_record(run_dir, index=index, patch=patch, metrics=metrics)
            except BaseException as update_error:
                _record_candidate_failure(
                    run_dir,
                    instance_id=iid,
                    phase=f"{WORKFLOW_NAME}_candidate_{index}_cleanup_record",
                    error=update_error,
                )
                raise
    return Candidate(index=index, patch=patch, tree=tree, metrics=metrics)


def _write_candidate_record(run_dir: Path, *, index: int, patch: str, metrics: dict) -> None:
    """Persist each candidate before winner selection.

    The winner is known only after all containers are finalized. Per-candidate
    records retain completed work if the run stops before then.
    """
    payload = {"candidate_index": index, "patch": patch, "metrics": metrics}
    write_regular_bytes_atomic(
        run_dir / "candidate.json",
        (json.dumps(payload, default=str) + "\n").encode("utf-8"),
    )


def _preserve_previous_candidate(run_dir: Path) -> None:
    """Keep the prior attempt before a stable candidate directory is reused."""
    source = run_dir / "candidate.json"
    try:
        with open_regular_binary(source) as handle:
            payload = handle.read(MAX_CANDIDATE_FILE_BYTES + 1)
    except FileNotFoundError:
        return
    if len(payload) > MAX_CANDIDATE_FILE_BYTES:
        raise OSError(f"previous candidate exceeds file budget: {source}")
    if not recover_generation_state(run_dir):
        raise RuntimeError(
            f"previous candidate and owned container need recovery in {run_dir}; "
            "inspect candidate.json and the container owner before retrying"
        )
    for attempt in count(1):
        archive = run_dir / f"candidate.previous-{attempt}.json"
        if archive.exists() or archive.is_symlink():
            continue
        write_regular_bytes_atomic(archive, payload, require_target_absent=True)
        print(f"  previous candidate preserved at {archive}; starting a new attempt")
        return


def _record_candidate_failure(
    run_dir: Path,
    *,
    instance_id: str,
    phase: str,
    error: BaseException,
    evidence: dict[str, object] | None = None,
) -> None:
    try:
        persist_generation_failure(
            run_dir,
            instance_id=instance_id,
            phase=phase,
            error=error,
            evidence=evidence,
        )
    except BaseException as record_error:
        add_exception_note(
            error,
            f"candidate failure record could not be written: {type(record_error).__name__}: {record_error}",
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
    pool: int | None,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """Build the submitted patch's metrics with totals across all candidates.

    Extraction and integrity proofs come from the selected candidate. Sum
    run_summary, used_tokens, and step_count across all candidates so losing
    candidates' usage remains counted. Preserve individual metrics under
    best_of_n.candidates.
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
            float((candidate.metrics.get(RUN_SUMMARY_KEY) or {}).get("duration_s") or 0.0) for candidate in candidates
        ),
        error=summary.get("error"),
    )
    metrics["step_count"] = steps_total
    metrics["used_tokens"] = tokens_total
    metrics["usage_complete"] = all(
        candidate.metrics.get("usage_complete", "used_tokens" in candidate.metrics) is True for candidate in candidates
    )
    metrics["used_tokens_lower_bound"] = not metrics["usage_complete"]
    metrics["budget"] = pool
    metrics["best_of_n"] = {
        **selection.as_dict(),
        "candidates_declared": declared_candidates,
        "candidates_completed": sum(1 for candidate in candidates if not candidate.metrics.get("error")),
        "candidate_budget": None if pool is None else pool // declared_candidates,
        "candidate_timeout_s": timeout / declared_candidates,
        "tokens_total": tokens_total,
        "steps_total": steps_total,
        "candidates": [_candidate_summary(candidate) for candidate in candidates],
    }
    return patch, metrics


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Generate one SWE-bench prediction with the Best-of-N arm")
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
    ap.add_argument(
        "--agent-profile", type=resolve_profile_name, default="base", help="Native agent profile for each candidate"
    )
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
    if args.budget is not None and args.budget // args.candidates < 1:
        ap.error("--budget leaves each candidate no tokens")
    args.max_steps, args.budget = resolve_agent_generation_limits(args.agent_profile, args.max_steps, args.budget)
    out_path, metrics_path = output_paths(args.output, args.metrics)

    instance = load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or default_container_image(args.arch, iid)

    cfg = get_config(
        str(Path.cwd()), overrides={"model": args.model, "provider": args.provider},
    )
    apply_model_overrides(cfg, args, ap)
    model_name = args.model_name or f"opencollab-{WORKFLOW_NAME}-{cfg['model']}"

    print(f"Instance:   {iid}")
    print(f"Image:      {image}")
    print(f"Model:      {cfg['model']} (provider={cfg['provider']})")
    print(
        f"Candidates: {args.candidates} x "
        f"{None if args.budget is None else args.budget // args.candidates} tokens, "
        f"{args.timeout / args.candidates:.0f}s each"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out_path.parent
    # Run candidates sequentially so the batch driver controls container
    # concurrency on the shared host.
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
            profile=args.agent_profile,
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
    print(f"Selected:   candidate {selection.index} by {selection.rule}{' (arbitrary)' if selection.arbitrary else ''}")

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
