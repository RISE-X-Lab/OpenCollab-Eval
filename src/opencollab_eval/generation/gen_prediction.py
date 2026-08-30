"""Generate a SWE-bench prediction with an OpenCollab agent.

Host-runnable bridge between the OpenCollab agent framework and the official
SWE-bench evaluation harness. For one SWE-bench instance it:

  1. starts the official ``sweb.eval`` image as a container (repo baked at
     /testbed, deps installed in the ``testbed`` conda env),
  2. runs a single OpenCollab agent inside it (edits + can run tests),
  3. captures ``git diff`` as the model patch,
  4. appends one ``{instance_id, model_name_or_path, model_patch}`` line to a
     predictions JSONL.

Grade the result with the official harness, e.g.::

    cd /path/to/swebench-eval
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-opencollab.jsonl -i sympy__sympy-20590 \
        -id oc-kimi --cache_level env --report_dir reports

Run with the OpenCollab venv (it must import ``opencollab``)::

    python -m opencollab_eval.generation.gen_prediction \
        --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
        --output /path/to/swebench-eval/predictions-opencollab.jsonl
"""

# ruff: noqa: F401

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import math
import operator
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import types
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

from opencollab_eval.engine.async_runtime import run_with_bounded_shutdown
from opencollab_eval.engine.swe_eval_records import (
    MAX_JSONL_SCAN_BYTES,
    open_regular_binary,
    read_bounded_json,
)
from opencollab_eval.runtime_config import resolve_runtime_config as get_config
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS, model_context_window

from . import (
    gen_prediction_agent,
    gen_prediction_config,
    gen_prediction_constants,
    gen_prediction_docker,
    gen_prediction_patch,
    gen_prediction_pending,
    gen_prediction_safe_output,
    gen_prediction_snapshot,
)
from .container_quiescence import container_image_id, require_container_quiescence
from .gen_prediction_agent import (
    build_task,
    load_instance,
    run_agent,
)
from .gen_prediction_config import (
    _docker_timeout_from_env,
    _stable_docker_component,
    _workspace_archive_timeout_from_env,
    bind_llm_transport,
    default_container_image,
    unique_container_name,
    validate_generation_limits,
    validate_instance_id,
)
from .gen_prediction_constants import (
    _ACTIVATE,
    _MISSING_CONTAINER_RE,
    AGENT_CANCELLATION_GRACE_SECONDS,
    AGENT_PROMPT,
    CONTAINER_OWNER_LABEL,
    CONTAINER_OWNER_SCHEMA_VERSION,
    DOCKER_WORKDIR,
    HARNESS_LOCK_TIMEOUT_SECONDS,
    MAX_CAPTURED_STDERR_BYTES,
    MAX_COMPATIBILITY_MARKER_BYTES,
    MAX_EXTRACTED_PATCH_BYTES,
    MAX_INSTANCE_BYTES,
    MAX_INSTANCE_ID_BYTES,
    MAX_JSONL_SCAN_LINE_BYTES,
    MAX_OUTPUT_JSONL_BYTES,
    MAX_OWNER_RECORD_BYTES,
    MAX_PENDING_OUTPUT_BYTES,
    MAX_STATUS_DIAGNOSTIC_BYTES,
    PENDING_OUTPUT_SCHEMA_VERSION,
    SAFE_FILE_OPEN_RETRIES,
    WORKFLOW_AGENT_PROMPT,
)
from .gen_prediction_docker import (
    _check_docker,
    _clear_compatibility_markers,
    _container_is_absent,
    _container_owner_label_state,
    _create_pending_owner,
    _docker,
    _encode_owner,
    _owner_directory,
    _owner_is_live,
    _owner_record,
    _path_matches_open_file,
    _process_start_identity,
    _read_owner,
    _read_small_regular_text,
    _remove_labeled_container,
    _remove_owned_container,
    _replace_owner,
    _require_creation_cleanup,
    _unlink_owner,
    _write_compatibility_markers,
    clear_container_marker,
    container_owner_path,
    finalize_container_ownership,
    mark_container_kept,
    recover_stale_container_owners,
    remove_container,
    remove_container_and_clear_marker,
    start_container,
    start_container_with_marker,
    write_container_marker,
)
from .gen_prediction_patch import extract_patch_guarded, prepare_trusted_patch_baseline
from .gen_prediction_pending import (
    _append_jsonl_durable_once,
    _candidate_matches_owner,
    _find_committed_identity,
    _open_pending_regular,
    _pending_output_directory,
    _pending_owner_state,
    _preservation_was_superseded,
    _promote_durable_preservation_candidates,
    _read_pending_fd,
    _row_output_identity,
    _unlink_pending_locked,
    _validate_pending_candidate,
    output_staging_requires_container_preservation,
    pending_output_path,
    persist_pending_output,
    publish_pending_output,
    recover_generation_state,
    require_published_output,
)
from .gen_prediction_safe_output import (
    _acquire_exclusive_lock,
    _append_jsonl_durable,
    _atomic_create_bytes,
    _atomic_write_bytes,
    _atomic_write_text,
    _cleanup_temporary_file,
    _fsync_directory,
    _open_regular_file,
    _patch_sha256,
    _validate_output_target,
    _write_all,
    append_output_records,
    build_output_records,
    complete_single_agent_integrity,
    default_metrics_path,
    metrics_have_completed_identity,
    normalize_trusted_extraction_status,
    output_paths,
    output_paths_collide,
    persist_generation_failure,
    raise_generation_failure_metrics,
    runner_returncode_for_metrics,
)
from .gen_prediction_snapshot import (
    SolverGitSnapshot,
    anonymous_solver_task_id,
    prepare_solver_git_snapshot,
)

_CONFIG_ROOT = Path(os.environ.get("OPENCOLLAB_EVAL_WORKSPACE", Path.cwd())).resolve()


def _cleanup_generation_attempt(
    *,
    trusted_baseline: object | None,
    run_dir: Path,
    cid: str,
    name: str,
    args: argparse.Namespace,
    metrics: dict,
    patch: str,
    pending_required: bool,
    pending_path: Path | None,
    generation_error: BaseException | None,
) -> tuple[tuple[str, BaseException], ...]:
    """Best-effort cleanup that never skips container finalization.

    Cleanup runs after both successful and failed generation.  Every cleanup
    operation is isolated so a baseline/evidence failure cannot mask the
    generation exception or prevent the ownership finalizer from running.
    """
    failures: list[tuple[str, BaseException]] = []

    def capture(label: str, error: BaseException) -> None:
        failures.append((label, error))
        detail = f"{type(error).__name__}: {str(error)[:512]}"
        if not isinstance(metrics, dict):
            return
        try:
            existing = metrics.get("cleanup_errors")
            if not isinstance(existing, list):
                existing = []
                metrics["cleanup_errors"] = existing
            existing.append(f"{label}: {detail}")
        except BaseException:
            # Metrics are diagnostic only; never let recording a cleanup error
            # prevent the remaining finalization attempt.
            pass

    if trusted_baseline is not None:
        try:
            trusted_baseline.cleanup()
        except BaseException as exc:
            capture("trusted baseline cleanup", exc)

    preserve_container = False
    try:
        preserve_container = bool(
            pending_required
            and pending_path is None
            and output_staging_requires_container_preservation(
                run_dir,
                cid=cid,
                name=name,
            )
        )
    except BaseException as exc:
        capture("container preservation check", exc)

    if preserve_container:
        try:
            metrics["container_preservation_required"] = True
        except BaseException as exc:
            capture("container preservation marker", exc)
            # If the preservation marker cannot be recorded, fail closed and
            # continue through ownership finalization instead of leaking the
            # active container.
            preserve_container = False
    if not preserve_container:
        completed = False
        try:
            completed = (
                not failures
                and generation_error is None
                and metrics_have_completed_identity(metrics, patch)
            )
        except BaseException as exc:
            capture("completion identity check", exc)
        try:
            finalize_container_ownership(
                run_dir=run_dir,
                cid=cid,
                name=name,
                keep_container=(
                    bool(getattr(args, "keep_container", False))
                    if generation_error is None and not failures
                    else False
                ),
                completed=completed,
                metrics=metrics,
            )
        except BaseException as exc:
            capture("container finalization", exc)

    return tuple(failures)


def _raise_or_note_cleanup_failures(
    failures: tuple[tuple[str, BaseException], ...],
    generation_error: BaseException | None,
) -> None:
    """Preserve generation errors while surfacing cleanup-only failures."""
    if not failures:
        return
    if generation_error is None:
        raise failures[0][1]
    add_note = getattr(generation_error, "add_note", None)
    for label, error in failures:
        note = (
            f"{label} failed after generation error: "
            f"{type(error).__name__}: {str(error)[:512]}"
        )
        if callable(add_note):
            try:
                add_note(note)
                continue
            except BaseException:
                pass
        try:
            notes = getattr(generation_error, "__notes__", None)
            if not isinstance(notes, list):
                notes = []
                generation_error.__notes__ = notes  # type: ignore[attr-defined]
            notes.append(note)
        except BaseException:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate one SWE-bench prediction with OpenCollab")
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
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--budget", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()
    try:
        args.max_steps, args.budget, args.timeout = validate_generation_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
        )
    except ValueError as exc:
        ap.error(str(exc))
    out_path, metrics_path = output_paths(args.output, args.metrics)

    instance = load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or default_container_image(args.arch, iid)

    cfg = get_config(str(_CONFIG_ROOT))
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
    model_name = args.model_name or f"opencollab-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")

    name = unique_container_name("oc-gen-", iid)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = out_path.parent
    cid = start_container_with_marker(image, name, run_dir)
    print(f"Container: {cid}")
    patch = ""
    metrics: dict = {}
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    trusted_baseline = None
    try:
        generation_image_id = container_image_id(cid)
        snapshot = prepare_solver_git_snapshot(
            cid,
            str(instance.get("base_commit") or ""),
        )
        trusted_baseline = prepare_trusted_patch_baseline(cid, snapshot)
        task = build_task(instance)
        metrics = run_with_bounded_shutdown(
            run_agent(
                task,
                cid,
                cfg,
                args.max_steps,
                args.budget,
                args.timeout,
                artifact_root=run_dir,
            )
        )
        metrics.update(
            {
                "llm_model": cfg["model"],
                "llm_provider": cfg["provider"],
                "context_window": model_context_window(cfg["model"]),
                "temperature": cfg.get("temperature"),
                "top_p": cfg.get("top_p"),
                "max_output_tokens": cfg.get(
                    "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
                "budget": args.budget,
                "max_steps": args.max_steps,
            }
        )
        bind_llm_transport(metrics)
        metrics["generation_image_id"] = generation_image_id
        metrics["solver_git_snapshot"] = snapshot.as_dict()
        if metrics.get("candidate_probe_eligible") is True:
            require_container_quiescence(cid)
            metrics["container_execution_quiesced"] = True
            metrics["execution_quiesced"] = (
                metrics.get("session_quiesced") is True
            )
            metrics["submission_eligible"] = (
                metrics["execution_quiesced"] is True
            )
            patch, removed_artifacts, extraction = extract_patch_guarded(cid, trusted_baseline)
            metrics["trusted_patch_extraction"] = extraction
            metrics["removed_generated_artifacts"] = removed_artifacts
            patch_extraction_succeeded = True
            normalize_trusted_extraction_status(metrics, patch)
        else:
            patch = ""
            patch_extraction_succeeded = False
            metrics["container_execution_quiesced"] = False
            metrics["execution_quiesced"] = False
            metrics["submission_eligible"] = False
        complete_single_agent_integrity(
            metrics,
            patch=patch,
            patch_extraction_succeeded=patch_extraction_succeeded,
        )
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        record, metric_record = build_output_records(
            instance_id=iid,
            model_name=model_name,
            patch=patch,
            metrics=metrics,
            workflow_name="single-agent",
        )
        pending_required = bool(patch.strip())
        if pending_required:
            pending_path = persist_pending_output(
                run_dir=run_dir,
                predictions_path=out_path,
                metrics_path=metrics_path,
                prediction=record,
                metric=metric_record,
                cid=cid,
                name=name,
            )
    except BaseException as exc:
        generation_error = exc
        persist_generation_failure(
            run_dir,
            instance_id=iid,
            phase="generation",
            error=exc,
        )
        raise
    finally:
        cleanup_failures = _cleanup_generation_attempt(
            trusted_baseline=trusted_baseline,
            run_dir=run_dir,
            cid=cid,
            name=name,
            args=args,
            metrics=metrics,
            patch=patch,
            pending_required=pending_required,
            pending_path=pending_path,
            generation_error=generation_error,
        )
        _raise_or_note_cleanup_failures(cleanup_failures, generation_error)

    if record is None or metric_record is None:
        raise RuntimeError("generation output record was not built")
    if pending_path is not None:
        publish_status = publish_pending_output(run_dir, pending_path)
        require_published_output(publish_status)
    else:
        append_output_records(out_path, metrics_path, record, metric_record)

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (agent made no tracked changes)")

    if not metrics_have_completed_identity(metric_record, patch):
        raise SystemExit(1)


_COMPATIBILITY_MODULES = (
    gen_prediction_constants,
    gen_prediction_config,
    gen_prediction_safe_output,
    gen_prediction_docker,
    gen_prediction_agent,
    gen_prediction_pending,
    gen_prediction_snapshot,
)


class _GenPredictionFacade(types.ModuleType):
    """Mirror compatibility patches into focused prediction helpers."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _GenPredictionFacade


if __name__ == "__main__":
    main()
