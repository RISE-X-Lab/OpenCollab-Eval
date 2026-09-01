"""Generate a SWE-bench prediction via the harness workflow mode (A/B driver).

Same container plumbing as ``gen_prediction.py`` (official ``sweb.eval`` image,
repo at /testbed, ``testbed`` conda env), but instead of one bespoke agent
session it drives ``run_eval_task(task, workflow=generate_review_fix)`` —
implement -> structured review verdict -> conditional fix — so the prediction
exercises the mini workflow engine end-to-end.

The current generator always uses blind validation and withholds official test
patches and FAIL_TO_PASS ids. Before Solver execution it captures an anonymous
Git baseline. After execution it copies a bounded workspace archive and uses a
clean host Git directory to extract the candidate against that fixed baseline.

Generate (OpenCollab venv, absolute paths in background shells)::

    python -m opencollab_eval.generation.gen_prediction_workflow \
        --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
        --output /path/to/swebench-eval/predictions-review-fix.jsonl

Grade with the official harness (separate venv)::

    cd /path/to/swebench-eval && HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python -m swebench.harness.run_evaluation \
        -p predictions-review-fix.jsonl -i sympy__sympy-20590 \
        -id review-fix-1 --cache_level env --report_dir reports
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import uuid
from dataclasses import fields
from pathlib import Path

from opencollab.environments import attach_container, build_repo_map_via_env
from opencollab.teams import declared_role_prompt_digests

from opencollab_eval import workflows as bundled_workflows
from opencollab_eval.engine.evaluator import EvalTask, run_eval_task  # noqa: E402
from opencollab_eval.engine.provider_failures import (  # noqa: E402
    summarize_terminal_provider_failures,
)
from opencollab_eval.engine.swe_eval_records import open_regular_binary  # noqa: E402
from opencollab_eval.engine.swe_generation_proof import (  # noqa: E402
    current_generation_proof_valid,
)
from opencollab_eval.patch_diff import (
    patch_paths as _patch_paths,
)
from opencollab_eval.runtime_config import resolve_runtime_config as get_config
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS, model_context_window

from . import gen_prediction as gp  # noqa: E402 — shared container plumbing
from .container_quiescence import require_container_quiescence  # noqa: E402
from .gen_prediction_constants import (  # noqa: E402
    DEFAULT_BUDGET,
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
)
from .gen_prediction_patch import extract_patch_guarded  # noqa: E402
from .gen_prediction_run_summary import (  # noqa: E402
    RUN_SUMMARY_KEY,
    build_run_summary,
)
from .gen_prediction_task_text import append_repository_layout  # noqa: E402
from .gen_prediction_workflow_inputs import (  # noqa: E402
    _blind_validation_default as _blind_validation_default,
)
from .gen_prediction_workflow_inputs import (  # noqa: E402
    _fail_to_pass_ids as _fail_to_pass_ids,
)
from .gen_prediction_workflow_inputs import (  # noqa: E402
    _resolve_blind_validation as _resolve_blind_validation,
)
from .gen_prediction_workflow_inputs import (  # noqa: E402
    _workflow_name as _workflow_name,
)
from .gen_prediction_workflow_inputs import build_extras as build_extras  # noqa: E402
from .gen_prediction_workflow_inputs import build_task as build_task  # noqa: E402
from .gen_prediction_workflow_inputs import json as json  # noqa: E402

_REPO_ROOT = Path(os.environ.get("OPENCOLLAB_EVAL_WORKSPACE", Path.cwd())).resolve()


def _bundled_workflow_registry() -> dict[str, object]:
    registry: dict[str, object] = {}
    for exported_name in bundled_workflows.__all__:
        workflow_fn = getattr(bundled_workflows, exported_name)
        spec = getattr(workflow_fn, "__workflow_spec__", None)
        public_name = getattr(spec, "name", None)
        if not isinstance(public_name, str) or not public_name:
            raise RuntimeError(
                f"bundled workflow {exported_name!r} has no public workflow name"
            )
        if public_name in registry:
            raise RuntimeError(f"duplicate bundled workflow name {public_name!r}")
        registry[public_name] = workflow_fn
    return registry


_BUNDLED_WORKFLOWS = _bundled_workflow_registry()


def validate_workflow_limits(
    *,
    max_steps: object,
    budget: object,
    timeout: object,
    checkpoint_interval: object,
) -> tuple[int, int, float, float]:
    normalized_steps, normalized_budget, normalized_timeout = (
        gp.validate_generation_limits(
            max_steps=max_steps,
            budget=budget,
            timeout=timeout,
        )
    )
    if isinstance(checkpoint_interval, bool):
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        )
    try:
        normalized_checkpoint = float(checkpoint_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        ) from exc
    if not math.isfinite(normalized_checkpoint) or normalized_checkpoint < 0:
        raise ValueError(
            "--checkpoint-interval-seconds must be a finite non-negative number"
        )
    return (
        normalized_steps,
        normalized_budget,
        normalized_timeout,
        normalized_checkpoint,
    )
from opencollab_eval.engine.workflows import generate_review_fix  # noqa: E402

# Team-baseline parity: use the current default per-instance cap for comparable
# OpenCollab SWE-bench runs.
DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 0.0
def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


def _patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


#: Fields the flat dump skips. ``patch`` is written separately (it is the
#: prediction, not a metric); ``tree_snapshots`` is written only when the arm
#: recorded any, so that a run of an arm that records no seat boundaries has no
#: key rather than a null one -- "this arm does not record boundaries" and "the
#: recorder produced nothing" would otherwise be the same row.
_METRICS_FLAT_DUMP_SKIPS = frozenset({"patch", "tree_snapshots"})


def _result_metrics(result) -> dict:
    metrics = {
        field.name: _json_safe(getattr(result, field.name))
        for field in fields(result)
        if field.name not in _METRICS_FLAT_DUMP_SKIPS
    }
    # The graded tree at each seat boundary, for the arms that record them.
    # A workflow arm carries its own equivalent inside ``workflow_result``
    # (``self_collaboration`` writes ``tree_snapshots`` there), so a reader that
    # wants either takes this key first and falls back to that one.
    if result.tree_snapshots is not None:
        metrics["tree_snapshots"] = _json_safe(result.tree_snapshots)
    # The same quantities again, under the names the single-agent path also
    # writes them under. Without this the two arms' records can only be read
    # one arm at a time; see ``gen_prediction_run_summary``.
    metrics[RUN_SUMMARY_KEY] = build_run_summary(
        steps=result.steps,
        tokens=result.tokens_used,
        status=result.runtime_status,
        reason=result.runtime_reason,
        duration_s=result.duration,
        error=result.error,
    )
    return metrics


def _verified_provider_models(
    trajectory_path: str | None,
    *,
    artifact_root: Path,
    expected_model: str,
    expected_reasoning_effort: str | None,
    wire_protocol: str,
) -> tuple[list[str], str | None]:
    if wire_protocol != "responses":
        return [], None
    if not trajectory_path:
        raise RuntimeError("Responses execution did not produce a trajectory")
    path = Path(trajectory_path)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(artifact_root.resolve(strict=True)):
            raise RuntimeError("Responses trajectory is outside the current artifact root")
        with open_regular_binary(path) as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read(16 * 1024 * 1024 + 1)
            after = os.fstat(handle.fileno())
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("Responses trajectory changed while reading")
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("Responses trajectory exceeds 16 MiB")
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Responses trajectory cannot be read") from exc
    models: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Responses trajectory contains invalid JSON") from exc
        if not isinstance(record, dict) or record.get("type") != "llm_call":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("Responses llm_call is missing its payload")
        if payload.get("wire_protocol") != "responses":
            raise RuntimeError("Responses trajectory contains a mixed wire protocol")
        observed = payload.get("provider_model")
        if observed != expected_model:
            raise RuntimeError(
                f"Responses provider model mismatch expected {expected_model!r} got {observed!r}"
            )
        observed_effort = payload.get("reasoning_effort")
        effort_policy = payload.get("reasoning_effort_policy")
        if effort_policy not in {"configured", "suppressed"}:
            raise RuntimeError("Responses llm_call is missing its reasoning effort policy")
        expected_effort = (
            None if effort_policy == "suppressed" else expected_reasoning_effort
        )
        if observed_effort != expected_effort:
            raise RuntimeError(
                "Responses reasoning effort mismatch "
                f"expected {expected_effort!r} got {observed_effort!r}"
            )
        models.append(observed)
    if not models:
        raise RuntimeError("Responses trajectory contains no verified LLM call")
    return sorted(set(models)), hashlib.sha256(raw).hexdigest()


def _workflow_status_for_result(result, patch: str) -> str:
    error = str(getattr(result, "error", None) or "")
    if error:
        if error.startswith("Task timed out after ") and patch.strip():
            return "done_with_timeout_patch"
        return "error"
    if not patch.strip():
        return "empty_patch_after_done"
    if getattr(result, "runtime_reason", None) == "timeout":
        return "done_with_timeout_patch"
    workflow_result = getattr(result, "workflow_result", None)
    if isinstance(workflow_result, dict) and workflow_result.get("status"):
        return str(workflow_result["status"])
    return "done" if patch.strip() else ""


def build_output_records(
    *,
    instance_id: str,
    model_name: str,
    patch: str,
    metrics: dict,
    workflow_name: str | None = None,
    record_id: str | None = None,
) -> tuple[dict, dict]:
    record_id = record_id or uuid.uuid4().hex
    patch_sha256 = _patch_sha256(patch)
    metric_record = {
        **metrics,
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha256,
        "model_name_or_path": model_name,
    }
    metric_record["runner_returncode"] = gp.runner_returncode_for_metrics(
        metric_record
    )
    prediction = {
        "instance_id": instance_id,
        "record_id": record_id,
        "patch_sha256": patch_sha256,
        "model_name_or_path": model_name,
        "model_patch": patch,
        "workflow_metric": metric_record,
    }
    if workflow_name:
        prediction["workflow"] = workflow_name
        metric_record["workflow"] = workflow_name
    return prediction, metric_record


def _patch_path_audit(patch: str) -> dict[str, object]:
    return {
        "actual_paths": sorted(set(_patch_paths(patch))),
        "selection_policy": "all_changes_against_verified_baseline",
    }


async def generate(
    instance: dict,
    image: str,
    cfg: dict,
    args: argparse.Namespace,
    workflow_fn,
    workflow_label: str | None = None,
    team_config: str | None = None,
) -> tuple[str, dict]:
    """Run the chosen solver in a fresh container; return (patch, metrics).

    ``team_config`` selects the team regime instead of a workflow: the same
    container, the same anonymous baseline and the same trusted host extraction,
    with the order of work decided by the model rather than by a script. The two
    are mutually exclusive and the caller has already rejected passing both.
    """
    iid = instance["instance_id"]
    name = gp.unique_container_name("oc-wf-", iid)
    run_dir = Path(args.output).parent
    cid = gp.start_container_with_marker(image, name, run_dir)
    print(f"Container: {cid}")
    patch = ""
    metrics: dict = {}
    output_path: Path | None = None
    metrics_path: Path | None = None
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    trusted_baseline = None
    try:
        generation_image_id = gp.container_image_id(cid)
        snapshot = gp.prepare_solver_git_snapshot(
            cid,
            str(instance.get("base_commit") or ""),
        )
        trusted_baseline = gp.prepare_trusted_patch_baseline(cid, snapshot)
        # Attach mode: run_eval_task's internal env.cleanup() no-ops on attached
        # containers, so the container survives for baseline-style extraction.
        env = attach_container(
            container_id=cid,
            workspace=gp.DOCKER_WORKDIR,
            command_prefix=gp._ACTIVATE,
            timeout_returncode=124,
        )

        async def env_factory(_task: EvalTask):
            return env

        blind_validation = _resolve_blind_validation(
            workflow_fn, getattr(args, "blind_validation", None), workflow_label
        )
        if not blind_validation:
            raise RuntimeError(
                "trusted host extraction requires blind validation without injected tests"
            )
        if bool(args.resume) or args.checkpoint_interval_seconds > 0:
            raise RuntimeError(
                "trusted host extraction does not accept container Git checkpoints"
            )
        include_hidden_tests = not blind_validation
        # Asked of the container, not walked here: the directory this process
        # could walk is the one the run was launched from, and the agents never
        # see it. The single-agent arm asks for the same listing the same way.
        repo_map = await build_repo_map_via_env(env)
        task = EvalTask(
            task_id=gp.anonymous_solver_task_id(),
            description=append_repository_layout(
                build_task(instance, include_fail_to_pass=include_hidden_tests),
                repo_map,
            ),
            timeout=args.timeout,
            max_tokens=args.budget,
            extras=build_extras(instance, include_hidden_tests=include_hidden_tests),
        )
        workflow_log_dir = Path(
            os.environ.get(
                "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR",
                str(_REPO_ROOT / "logs" / "eval_workflow"),
            )
        ).resolve()
        result = await run_eval_task(
            task,
            model=cfg["model"],
            provider=cfg["provider"],
            output_dir=str(workflow_log_dir),
            prompt=gp.WORKFLOW_AGENT_PROMPT,
            env_factory=env_factory,
            max_steps=args.max_steps,
            workflow=None if team_config else workflow_fn,
            team_config=team_config,
            temperature=cfg["temperature"],
            top_p=cfg.get("top_p"),
            max_output_tokens=cfg.get(
                "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
            ),
            thinking=cfg.get("thinking", False),
            thinking_params=cfg.get("thinking_params") or None,
            wire_protocol=cfg.get("wire_protocol", "chat_completions"),
            reasoning_effort=cfg.get("reasoning_effort"),
            llm_connect_timeout=cfg.get("llm_connect_timeout", 30.0),
            llm_first_event_timeout=cfg.get("llm_first_event_timeout", 180.0),
            llm_stream_idle_timeout=cfg.get("llm_stream_idle_timeout", 180.0),
            checkpoint_interval_seconds=None,
            resume_from_checkpoint=False,
            defer_patch_extraction=True,
        )
        require_container_quiescence(cid)
        print(
            f"  workflow: tokens={result.tokens_used} steps={result.steps} "
            f"duration={result.duration:.0f}s error={result.error}"
        )
        provider_failure = summarize_terminal_provider_failures(
            result.agent_failures
        )
        try:
            provider_models, trajectory_sha256 = _verified_provider_models(
                result.trajectory_path,
                artifact_root=workflow_log_dir / "trajectories" / task.task_id,
                expected_model=cfg["model"],
                expected_reasoning_effort=cfg.get("reasoning_effort"),
                wire_protocol=cfg.get("wire_protocol", "chat_completions"),
            )
        except RuntimeError as exc:
            result.error = str(exc)
            provider_models = []
            trajectory_sha256 = None
        outer_extraction_allowed = bool(
            result.execution_quiesced
            and result.injected_path_cleanup_proven
            and result.harness_artifact_exclusion_proven
            and result.checkpoint_restore_integrity_proven
            and result.task_stage_integrity_proven
            and not result.test_patch_isolation_failed
            and not result.error
            and provider_failure is None
        )
        if outer_extraction_allowed:
            patch, removed_validation_artifacts, extraction_proof = extract_patch_guarded(
                cid,
                trusted_baseline,
            )
        else:
            patch = ""
            removed_validation_artifacts = []
            extraction_proof = None
        metrics = _result_metrics(result)
        if provider_failure is not None:
            metrics["provider_failure"] = provider_failure
        metrics["container_execution_quiesced"] = True
        metrics["execution_quiesced"] = metrics.get("execution_quiesced") is True
        metrics["submission_eligible"] = (
            metrics.get("submission_eligible") is True
            and metrics["execution_quiesced"] is True
        )
        metrics.update(
            {
                "llm_model": cfg["model"],
                "provider_models": provider_models,
                "trajectory_sha256": trajectory_sha256,
                "llm_provider": cfg["provider"],
                "wire_protocol": cfg.get("wire_protocol", "chat_completions"),
                "reasoning_effort": cfg.get("reasoning_effort"),
                "context_window": model_context_window(cfg["model"]),
                "temperature": cfg["temperature"],
                "top_p": cfg.get("top_p"),
                "max_output_tokens": cfg.get(
                    "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
                "budget": args.budget,
                "max_steps": args.max_steps,
                "llm_base_url_sha256": cfg.get("base_url_sha256"),
                "workflow_env": {
                    key: os.environ[key]
                    for key in (
                        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
                        "OPENCOLLAB_EVAL_WORKFLOW_CONCURRENCY",
                        "OPENCOLLAB_TEMPERATURE",
                        "OPENCOLLAB_THINKING",
                        "OPENCOLLAB_THINKING_PARAMS",
                        "OPENCOLLAB_TOP_P",
                        "OPENCOLLAB_WIRE_PROTOCOL",
                        "OPENCOLLAB_REASONING_EFFORT",
                        "OPENCOLLAB_LLM_MAX_RETRIES",
                        "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
                        "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT",
                        "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
                        "OPENCOLLAB_LLM_USER_AGENT",
                        "OPENCOLLAB_WORKSPACE_ARCHIVE_TIMEOUT",
                    )
                    if key in os.environ
                },
            }
        )
        gp.bind_llm_transport(metrics)
        if team_config is not None:
            # The grouping key of the handoff experiment's estimand: which cards
            # this run sat with, by content rather than by path (see
            # ``declared_role_prompt_digests``). Team regime only -- an arm that
            # seats no cards must not read like one whose recorder broke.
            metrics["team_config_path"] = team_config
            metrics["role_prompt_sha256"] = declared_role_prompt_digests(team_config)
        metrics["generation_image_id"] = generation_image_id
        metrics["solver_git_snapshot"] = snapshot.as_dict()
        if extraction_proof is not None:
            metrics["trusted_patch_extraction"] = extraction_proof
            metrics["patch_path_audit"] = _patch_path_audit(patch)
        extraction_valid = current_generation_proof_valid(metrics, patch)
        metrics["patch_extraction_succeeded"] = extraction_valid
        metrics["task_stage_integrity_proven"] = extraction_valid
        metrics["worktree_integrity_proven"] = extraction_valid
        metrics["submission_eligible"] = (
            outer_extraction_allowed and extraction_valid and bool(patch.strip())
        )
        metrics["patch_produced"] = bool(patch.strip())
        metrics["submitted_patch_chars"] = len(patch)
        if provider_failure is not None:
            metrics["workflow_status"] = "provider_request_rejected"
        elif not metrics.get("workflow_status"):
            metrics["workflow_status"] = _workflow_status_for_result(result, patch)
        gp.normalize_trusted_extraction_status(metrics, patch)
        if removed_validation_artifacts:
            metrics["validation_artifacts_removed"] = removed_validation_artifacts
        if getattr(args, "_persist_output_after_cleanup", False):
            output_path = Path(args.output)
            metrics_path_arg = getattr(args, "metrics", None)
            metrics_path = (
                Path(metrics_path_arg)
                if metrics_path_arg
                else gp.default_metrics_path(output_path)
            )
            persisted_model_name = getattr(args, "model_name", None) or (
                f"opencollab-{_workflow_name(workflow_fn, workflow_label)}-{cfg['model']}"
            )
            record, metric_record = build_output_records(
                instance_id=iid,
                model_name=persisted_model_name,
                patch=patch,
                metrics=metrics,
                workflow_name=_workflow_name(workflow_fn, workflow_label),
            )
            pending_required = bool(patch.strip())
            if pending_required:
                pending_path = gp.persist_pending_output(
                    run_dir=run_dir,
                    predictions_path=output_path,
                    metrics_path=metrics_path,
                    prediction=record,
                    metric=metric_record,
                    cid=cid,
                    name=name,
                )
    except BaseException as exc:
        generation_error = exc
        gp.persist_generation_failure(
            run_dir,
            instance_id=iid,
            phase="workflow_generation",
            error=exc,
        )
        raise
    finally:
        if trusted_baseline is not None:
            trusted_baseline.cleanup()
        preserve_container = (
            pending_required
            and pending_path is None
            and gp.output_staging_requires_container_preservation(
                run_dir,
                cid=cid,
                name=name,
            )
        )
        if preserve_container:
            metrics["container_preservation_required"] = True
        else:
            completed = generation_error is None and gp.metrics_have_completed_identity(
                metrics,
                patch,
            )
            try:
                gp.finalize_container_ownership(
                    run_dir=run_dir,
                    cid=cid,
                    name=name,
                    keep_container=args.keep_container if generation_error is None else False,
                    completed=completed,
                    metrics=metrics,
                )
            except BaseException as cleanup_error:
                if generation_error is None:
                    raise
                add_note = getattr(generation_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "container cleanup failed after generation error: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )

    if getattr(args, "_persist_output_after_cleanup", False):
        if (
            output_path is None
            or metrics_path is None
            or record is None
            or metric_record is None
        ):
            raise RuntimeError("workflow output record was not built")
        if pending_path is not None:
            publish_status = gp.publish_pending_output(run_dir, pending_path)
            gp.require_published_output(
                publish_status,
                label="pending workflow output",
            )
        else:
            gp.append_output_records(output_path, metrics_path, record, metric_record)
    return patch, metrics


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one SWE-bench prediction with the review-fix workflow"
    )
    ap.add_argument("--instance-file", required=True, help="JSON file with one instance")
    ap.add_argument("--output", required=True, help="Predictions JSONL to append to")
    ap.add_argument("--metrics", default=None,
                    help="Metrics JSONL to append to (default: metrics.jsonl beside --output)")
    ap.add_argument("--image", default=None, help="Override container image")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--top-p", type=float)
    ap.add_argument("--max-output-tokens", type=int)
    ap.add_argument("--model-name", default=None, help="model_name_or_path in predictions")
    solver_group = ap.add_mutually_exclusive_group()
    solver_group.add_argument("--workflow", default=None,
                              help="Bundled workflow name (e.g. analyst-solve); "
                                   "default: the built-in generate_review_fix")
    solver_group.add_argument(
        "--team-config", default=None,
        help="Team file to run instead of a workflow. Selects the regime whose "
             "order of work the model decides; the path is site-specific and "
             "has no default.",
    )
    blind_group = ap.add_mutually_exclusive_group()
    blind_group.add_argument("--blind-validation", dest="blind_validation",
                             action="store_true",
                             help="Do not inject official test_patch or FAIL_TO_PASS ids")
    blind_group.add_argument("--with-hidden-tests", dest="blind_validation",
                             action="store_false",
                             help="Inject official test_patch and FAIL_TO_PASS ids")
    ap.set_defaults(blind_validation=True)
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                    help="Step cap per workflow session")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="Shared token budget across all workflow sessions")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument(
        "--checkpoint-interval-seconds",
        type=float,
        default=DEFAULT_CHECKPOINT_INTERVAL_SECONDS,
        help="Must remain 0 for trusted host extraction; positive values are rejected",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Unsupported in trusted host extraction mode; passing it is rejected",
    )
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()
    try:
        (
            args.max_steps,
            args.budget,
            args.timeout,
            args.checkpoint_interval_seconds,
        ) = validate_workflow_limits(
            max_steps=args.max_steps,
            budget=args.budget,
            timeout=args.timeout,
            checkpoint_interval=args.checkpoint_interval_seconds,
        )
    except (TypeError, ValueError) as exc:
        ap.error(str(exc))
    out_path, _metrics_path = gp.output_paths(args.output, args.metrics)

    instance = gp.load_instance(args.instance_file)
    iid = instance["instance_id"]
    image = args.image or f"sweb.eval.{args.arch}.{iid}:latest"

    # Resolve the solver: a team file, a named bundled workflow, or the
    # built-in fallback.
    if args.team_config:
        team_config_path = Path(args.team_config).expanduser()
        if not team_config_path.is_file():
            ap.error(f"--team-config is not a file: {team_config_path}")
        workflow_fn, wf_label = None, "team"
    elif args.workflow:
        try:
            workflow_fn = _BUNDLED_WORKFLOWS[args.workflow]
        except KeyError:
            names = ", ".join(sorted(_BUNDLED_WORKFLOWS)) or "(none)"
            ap.error(f"unknown --workflow {args.workflow!r}; available: {names}")
        wf_label = args.workflow
        team_config_path = None
    else:
        workflow_fn, wf_label = generate_review_fix, "generate_review_fix"
        team_config_path = None
    args.blind_validation = _resolve_blind_validation(workflow_fn, args.blind_validation, wf_label)

    cfg = get_config(str(_REPO_ROOT))
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
    model_name = args.model_name or f"opencollab-{wf_label}-{cfg['model']}"

    print(f"Instance: {iid}")
    print(f"Image:    {image}")
    print(f"Model:    {cfg['model']} (provider={cfg['provider']})")
    print(f"Thinking: {cfg.get('thinking', False)}")
    print(f"Solver:   {wf_label} (budget={args.budget}, "
          f"max_steps/session={args.max_steps})")
    if team_config_path is not None:
        print(f"Team:     {team_config_path}")
    print(f"Blind validation: {args.blind_validation}")
    print(
        "Checkpoint: "
        f"{args.checkpoint_interval_seconds:g}s"
        f"{' resume' if args.resume else ''}"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    args.model_name = model_name
    args._persist_output_after_cleanup = True
    patch, metrics = gp.run_with_bounded_shutdown(
        generate(
            instance,
            image,
            cfg,
            args,
            workflow_fn,
            wf_label,
            team_config=None if team_config_path is None else str(team_config_path),
        )
    )

    if patch.strip():
        print(f"\nPatch ({len(patch)} chars) written to {out_path}")
        print("--- patch preview ---")
        print("\n".join(patch.splitlines()[:40]))
    else:
        print("\nWARNING: empty patch (the solver made no tracked changes)")

    if not gp.metrics_have_completed_identity(metrics, patch):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
