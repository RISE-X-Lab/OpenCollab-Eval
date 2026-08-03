"""Adapt an external solver to trusted SWE prediction and metrics records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from opencollab_eval.benchmarks.task_specification import (  # noqa: E402
    compose_task_specification,
)
from opencollab_eval.benchmarks.task_specification import (  # noqa: E402
    solver_task_instance as _solver_instance,
)
from opencollab_eval.engine.swe_generation_proof import (  # noqa: E402
    current_generation_proof_valid,
)
from opencollab_eval.engine.swe_v1_remote_records import read_tail_text  # noqa: E402

from . import claude_code_sidecar as ccs  # noqa: E402
from . import container_quiescence as container_guard  # noqa: E402
from . import gen_prediction as gp  # noqa: E402
from . import openhands_events  # noqa: E402
from .external_solver_containers import cleanup_external_solver_containers  # noqa: E402
from .external_solver_usage import (  # noqa: E402
    _append_usage_record,
    _bind_external_solver_evidence,
    _external_solver_evidence,
    _external_solver_usage,
    _external_solver_usage_evidence,
    _openhands_usage,
)
from .gen_prediction_patch import extract_patch_guarded, prepare_trusted_patch_baseline  # noqa: E402
from .gen_prediction_snapshot import (  # noqa: E402
    anonymous_solver_task_id,
    prepare_solver_git_snapshot,
)
from .gen_prediction_workflow import (  # noqa: E402
    _patch_path_audit,
    build_output_records,
)

DEFAULT_PROMPT = """\
# Issue to fix in `{repo}`

{problem_statement}
{hints_block}
## Workspace
Your terminal is already bound to an isolated, offline workspace at `{workspace}`.
Run repository reads, searches, edits, and tests directly in that terminal. Before
finishing, run `git status --short` and confirm that tracked source files contain
the intended changes.

Fix the source root cause with a minimal patch. Do not edit benchmark tests, do
not run git commit, and leave all source changes in the working tree.
"""
_CONTAINER_GUARD_ROOT = container_guard.CONTAINER_GUARD_ROOT


def _prompt(instance: dict, *, container_id: str) -> str:
    hints = str(instance.get("hints_text") or "").strip()
    hints_block = f"\n## Hints\n{hints}\n" if hints else "\n"
    return DEFAULT_PROMPT.format(
        repo=instance.get("repo") or "",
        problem_statement=compose_task_specification(instance),
        hints_block=hints_block,
        container_id=container_id,
        workspace=gp.DOCKER_WORKDIR,
    )


def _template_values(
    *,
    container_id: str,
    instance_id: str,
    instance_file: Path,
    prompt_file: Path,
    output_dir: Path,
    timeout: float,
) -> dict[str, str]:
    raw = {
        "container_id": container_id,
        "workspace": gp.DOCKER_WORKDIR,
        "instance_id": instance_id,
        "instance_file": str(instance_file),
        "prompt_file": str(prompt_file),
        "output_dir": str(output_dir),
        "timeout": str(int(timeout)),
    }
    return {key: shlex.quote(value) for key, value in raw.items()}


def _format_command(template: str, values: dict[str, str]) -> str:
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise SystemExit(f"unknown OpenHands command placeholder: {exc.args[0]}") from exc


def _stop_hook_command() -> str:
    python_bin = shlex.quote(sys.executable)
    return (
        f"{python_bin} -m opencollab_eval.generation.openhands_require_patch "
        "|| exit 1"
    )


def _read_log_tail(path: Path, *, max_bytes: int = 1024 * 1024) -> str:
    return read_tail_text(path, max_bytes)


def _complete_openhands_integrity(
    metrics: dict,
    *,
    patch: str,
    snapshot_prepared: bool,
    process_quiesced: bool,
    patch_extraction_succeeded: bool,
    harness_artifact_exclusion_proven: bool,
) -> None:
    patch_produced = bool(patch.strip())
    trusted_extraction_proven = (
        snapshot_prepared
        and patch_extraction_succeeded
        and current_generation_proof_valid(metrics, patch)
    )
    task_stage_integrity_proven = trusted_extraction_proven
    worktree_integrity_proven = (
        trusted_extraction_proven
        and harness_artifact_exclusion_proven
    )
    submission_eligible = (
        metrics.get("workflow_status") == "done"
        and patch_produced
        and process_quiesced
        and worktree_integrity_proven
    )
    metrics.update(
        {
            "submission_eligible": submission_eligible,
            "execution_quiesced": process_quiesced,
            "patch_extraction_succeeded": trusted_extraction_proven,
            # OpenHands receives no benchmark test patch, so there are no
            # injected paths or checkpoint mutations to restore.
            "injected_path_cleanup_proven": trusted_extraction_proven,
            "harness_artifact_exclusion_proven": (
                harness_artifact_exclusion_proven
            ),
            "checkpoint_restore_integrity_proven": trusted_extraction_proven,
            "task_stage_integrity_proven": task_stage_integrity_proven,
            "test_patch_isolation_failed": False,
            "worktree_integrity_proven": worktree_integrity_proven,
            "patch_produced": patch_produced,
        }
    )


def _supervisor_proved_quiescence(returncode: int) -> bool:
    return returncode >= 0 and returncode != 125


def _prepare_openhands_container_guard(container_id: str) -> str:
    return container_guard.prepare_container_guard(container_id)


def _quiesce_openhands_container(container_id: str, python_bin: str) -> dict[str, object]:
    return container_guard.quiesce_container(
        container_id,
        python_bin,
        cleanup_guard_root=True,
    )


def _openhands_patch_extraction_allowed(metrics: dict) -> bool:
    return (
        metrics.get("status") == "done"
        and metrics.get("execution_quiesced") is True
        and metrics.get("host_execution_quiesced") is True
        and metrics.get("container_execution_quiesced") is True
    )


def _run_openhands(
    *,
    command_template: str,
    container_id: str,
    instance: dict,
    instance_file: Path,
    prompt_file: Path,
    output_dir: Path,
    timeout: float,
    context_window: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    token_budget: int | None = None,
    max_steps: int | None = None,
    empty_patch_rejections: int = 0,
    removed_gitlinks: tuple[tuple[str, str], ...] = (),
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    container_python = _prepare_openhands_container_guard(container_id)
    command = _format_command(
        command_template,
        _template_values(
            container_id=container_id,
            instance_id=instance["instance_id"],
            instance_file=instance_file,
            prompt_file=prompt_file,
            output_dir=output_dir,
            timeout=timeout,
        ),
    )
    inherited_names = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "PATH",
        "PYTHONPATH",
        "TERM",
        "TMPDIR",
        "USER",
        "OPENCOLLAB_OPENHANDS_PYTHON",
        "OPENCOLLAB_REMOTE_REPO", *ccs.RUNTIME_ENV_KEYS,
    }
    env = {name: os.environ[name] for name in inherited_names if name in os.environ}
    env.update(
        {
            "OPENHANDS_CONTAINER_ID": container_id,
            "OPENHANDS_WORKSPACE": gp.DOCKER_WORKDIR,
            "OPENHANDS_INSTANCE_ID": instance["instance_id"],
            "OPENHANDS_INSTANCE_FILE": str(instance_file),
            "OPENHANDS_PROMPT_FILE": str(prompt_file),
            "OPENHANDS_OUTPUT_DIR": str(output_dir),
            "OPENHANDS_TIMEOUT": str(int(timeout)),
            "OPENHANDS_PERSISTENCE_DIR": str(output_dir / "persistence"),
            "OPENHANDS_CONVERSATIONS_DIR": str(output_dir / "persistence" / "conversations"),
            "OPENHANDS_WORK_DIR": str(output_dir),
            "OPENHANDS_CONTAINER_PYTHON": container_python,
            "OPENHANDS_CONTAINER_GUARD_ROOT": _CONTAINER_GUARD_ROOT,
            "OPENHANDS_REMOVED_GITLINKS_JSON": json.dumps(
                [
                    {"path": path, "old_oid": old_oid}
                    for path, old_oid in removed_gitlinks
                ],
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        }
    )
    runtime_values = {
        "OPENHANDS_CONTEXT_WINDOW": context_window,
        "OPENHANDS_TEMPERATURE": temperature,
        "OPENHANDS_TOP_P": top_p,
        "OPENHANDS_MAX_OUTPUT_TOKENS": max_output_tokens,
        "OPENHANDS_TOKEN_BUDGET": token_budget,
        "OPENHANDS_MAX_STEPS": max_steps,
        "OPENHANDS_EMPTY_PATCH_REJECTIONS": empty_patch_rejections,
    }
    env.update(
        {key: str(value) for key, value in runtime_values.items() if value is not None}
    )
    command_log = output_dir / "openhands.command.txt"
    stdout_log = output_dir / "openhands.stdout.log"
    stderr_log = output_dir / "openhands.stderr.log"
    command_log.write_text(command + "\n", encoding="utf-8")
    started = time.time()
    timed_out = False
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        supervised_command = [
            sys.executable,
            "-m",
            "opencollab_eval.generation.openhands_process_supervisor",
            "--timeout-seconds",
            str(timeout),
            "--",
            "/bin/sh",
            "-c",
            command,
        ]
        proc = subprocess.Popen(
            supervised_command,
            shell=False,
            cwd=str(output_dir),
            env=env,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=timeout + container_guard.KILL_GRACE_SECONDS + 1.0)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.send_signal(signal.SIGTERM)
            try:
                supervisor_returncode = proc.wait(timeout=container_guard.KILL_GRACE_SECONDS + 1.0)
            except subprocess.TimeoutExpired:
                host_supervisor_cleanup_error = container_guard.terminate_supervisor_process(proc)
                host_cleanup_quiesced = False
                returncode = 125
            else:
                host_cleanup_quiesced = _supervisor_proved_quiescence(
                    supervisor_returncode
                )
                returncode = 124 if host_cleanup_quiesced else 125
        else:
            host_cleanup_quiesced = _supervisor_proved_quiescence(returncode)
            timed_out = returncode == 124
    try:
        external_container_cleanup = cleanup_external_solver_containers(output_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        external_container_cleanup = {
            "proven": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    host_cleanup_quiesced = (
        host_cleanup_quiesced and external_container_cleanup.get("proven") is True
    )
    container_quiescence = _quiesce_openhands_container(
        container_id,
        container_python,
    )
    container_cleanup_quiesced = container_quiescence["proven"] is True
    cleanup_quiesced = host_cleanup_quiesced and container_cleanup_quiesced
    stderr_tail = _read_log_tail(stderr_log)
    external_solver_evidence = None
    external_solver_usage_evidence = None
    external_solver_error = None
    try:
        external_solver_evidence = _external_solver_evidence(output_dir)
    except ValueError as exc:
        external_solver_error = str(exc)
    try:
        external_solver_usage_evidence = _external_solver_usage_evidence(output_dir)
    except ValueError:
        external_solver_usage_evidence = None
    if not cleanup_quiesced:
        status = "openhands_cleanup_failed"
        returncode = 125
    elif external_solver_error is not None:
        status = "external_solver_evidence_failed"
        returncode = 126
    elif timed_out:
        status = "openhands_timeout"
    elif returncode == 125:
        status = "openhands_supervision_failed"
    else:
        status = "done" if returncode == 0 else "openhands_failed"
        if returncode == 0 and any(
            marker in stderr_tail
            for marker in (
                "Traceback (most recent call last)",
                "ModuleNotFoundError:",
                "ImportError:",
            )
        ):
            status = "openhands_failed"
    result = {
        "status": status,
        "returncode": returncode,
        "duration_s": round(time.time() - started, 3),
        "execution_quiesced": cleanup_quiesced,
        "host_execution_quiesced": host_cleanup_quiesced,
        "host_supervisor_cleanup_error": locals().get("host_supervisor_cleanup_error", ""),
        "container_execution_quiesced": container_cleanup_quiesced,
        "container_quiescence_returncode": container_quiescence["returncode"],
        "container_quiescence_error": container_quiescence["error"],
        "external_container_cleanup": external_container_cleanup,
        "command_log": str(command_log),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    if external_solver_evidence is not None:
        result["external_solver"] = external_solver_evidence["solver"]
        result["external_solver_evidence"] = external_solver_evidence
    elif external_solver_usage_evidence is not None:
        result["external_solver"] = external_solver_usage_evidence["solver"]
    if external_solver_usage_evidence is not None:
        result["external_solver_usage_evidence"] = external_solver_usage_evidence
    if external_solver_error is not None:
        result["external_solver_error"] = external_solver_error
    result.update(openhands_events.terminal_error_evidence(stdout_log))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one SWE prediction with external OpenHands")
    parser.add_argument("--instance-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metrics")
    parser.add_argument("--image")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--model-name", default="openhands")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--empty-patch-rejections", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=14_400)
    parser.add_argument("--command", default=os.environ.get("OPENCOLLAB_OPENHANDS_COMMAND", ""))
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--dry-run-command", action="store_true")
    args = parser.parse_args()

    if not args.command:
        raise SystemExit(
            "missing OpenHands command. Set OPENCOLLAB_OPENHANDS_COMMAND or pass --command."
        )

    instance_file = Path(args.instance_file)
    instance = json.loads(instance_file.read_text(encoding="utf-8"))
    instance_id = instance["instance_id"]
    solver_task_id = anonymous_solver_task_id()
    image = args.image or f"sweb.eval.{args.arch}.{instance_id}:latest"
    run_dir = Path(args.output).parent
    evidence_dir = run_dir / "openhands_attempts" / solver_task_id
    removed_validation_artifacts: list[str] = []
    patch = ""
    metrics: dict = {}
    snapshot_prepared = False
    process_quiesced = False
    patch_extraction_succeeded = False
    harness_artifact_exclusion_proven = False
    record: dict | None = None
    metric_record: dict | None = None
    pending_path: Path | None = None
    pending_required = False
    generation_error: BaseException | None = None
    trusted_baseline = None

    name = gp.unique_container_name("oc-oh-", solver_task_id)
    cid = gp.start_container_with_marker(image, name, run_dir)
    openhands_dir = Path(tempfile.mkdtemp(prefix="opencollab-openhands-"))
    print(f"Instance: {instance_id}")
    print(f"Image:    {image}")
    print(f"Container: {cid}")
    try:
        generation_image_id = container_guard.container_image_id(cid)
        snapshot_evidence = prepare_solver_git_snapshot(cid, instance["base_commit"])
        snapshot_prepared = True
        trusted_baseline = prepare_trusted_patch_baseline(cid, snapshot_evidence)
        prompt_file = openhands_dir / "prompt.md"
        openhands_dir.mkdir(parents=True, exist_ok=True)
        solver_instance_file = openhands_dir / "solver_instance.json"
        solver_instance = _solver_instance(instance, solver_task_id)
        solver_instance_file.write_text(
            json.dumps(solver_instance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        hooks_dir = openhands_dir / ".openhands"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(
                {
                    "stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _stop_hook_command(),
                                    "timeout": 45,
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        prompt_file.write_text(_prompt(instance, container_id=cid), encoding="utf-8")
        values = _template_values(
            container_id=cid,
            instance_id=solver_task_id,
            instance_file=solver_instance_file,
            prompt_file=prompt_file,
            output_dir=openhands_dir,
            timeout=args.timeout,
        )
        if args.dry_run_command:
            rendered = _format_command(args.command, values)
            print(rendered)
            metrics = {
                "generator": "openhands",
                "generation_image_id": generation_image_id,
                "solver_git_snapshot": snapshot_evidence.as_dict(),
                "openhands_status": "dry_run",
                "workflow_status": "dry_run",
                "patch_produced": False,
                "submitted_patch_chars": 0,
            }
            patch = ""
            process_quiesced = True
        else:
            metrics = {
                "generator": "openhands",
                "generation_image_id": generation_image_id,
                "solver_git_snapshot": snapshot_evidence.as_dict(),
                **_run_openhands(
                    command_template=args.command,
                    container_id=cid,
                    instance=solver_instance,
                    instance_file=solver_instance_file,
                    prompt_file=prompt_file,
                    output_dir=openhands_dir,
                    timeout=args.timeout,
                    context_window=args.context_window,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    max_output_tokens=args.max_output_tokens,
                    token_budget=args.budget,
                    max_steps=args.max_steps,
                    empty_patch_rejections=max(0, args.empty_patch_rejections),
                    removed_gitlinks=snapshot_evidence.removed_gitlinks,
                ),
            }
            external_solver = metrics.get("external_solver")
            if isinstance(external_solver, str) and external_solver:
                metrics["generator"] = external_solver
            process_quiesced = _openhands_patch_extraction_allowed(metrics)
            if process_quiesced:
                try:
                    (
                        patch,
                        removed_validation_artifacts,
                        extraction_proof,
                    ) = extract_patch_guarded(
                        cid,
                        trusted_baseline,
                    )
                except Exception as exc:
                    generation_error = RuntimeError(
                        "OpenHands patch extraction or validation-artifact cleanup failed"
                    )
                    generation_error.__cause__ = exc
                    metrics["patch_guard_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    metrics["workflow_status"] = "patch_guard_failed"
                    patch = ""
                else:
                    metrics["trusted_patch_extraction"] = extraction_proof
                    metrics["patch_path_audit"] = _patch_path_audit(patch)
                    external_evidence = metrics.get("external_solver_evidence")
                    if isinstance(external_evidence, dict):
                        try:
                            _bind_external_solver_evidence(
                                external_evidence,
                                output_dir=openhands_dir,
                                prompt_file=openhands_dir / "claude.prompt.md",
                                solver_task_id=solver_task_id,
                                public_instance_id=instance_id,
                                anonymous_head=snapshot_evidence.anonymous_head,
                                base_tree=snapshot_evidence.base_tree,
                                trusted_extraction=extraction_proof,
                                baseline_git_dir=trusted_baseline.git_dir,
                                patch=patch,
                                run_identity={
                                    key: os.environ[key]
                                    for key in (
                                        "OPENCOLLAB_EVAL_INVOCATION_ID",
                                        "OPENCOLLAB_EVAL_RUN_ID",
                                        "OPENCOLLAB_RUNTIME_TREE_SHA256",
                                    )
                                    if key in os.environ
                                },
                            )
                        except (OSError, ValueError) as exc:
                            generation_error = RuntimeError(
                                "external solver candidate binding failed"
                            )
                            generation_error.__cause__ = exc
                            metrics["workflow_status"] = "external_solver_binding_failed"
                        else:
                            patch_extraction_succeeded = True
                            harness_artifact_exclusion_proven = True
                    else:
                        patch_extraction_succeeded = True
                        harness_artifact_exclusion_proven = True
            elif metrics.get("execution_quiesced") is not True:
                generation_error = RuntimeError("OpenHands execution cleanup did not quiesce")
            else:
                generation_error = RuntimeError(
                    f"OpenHands generation command failed with status {metrics.get('status', 'unknown')}"
                )
            metrics["submitted_patch_chars"] = len(patch)
            generation_error = generation_error or openhands_events.apply_empty_patch_failure(metrics, patch)
            if "workflow_status" not in metrics and metrics.get("status") == "done":
                metrics["workflow_status"] = (
                    "done" if patch.strip() else "empty_patch_after_done"
                )
            elif "workflow_status" not in metrics:
                metrics["workflow_status"] = "error"
            usage_values = _external_solver_usage(
                metrics.get("external_solver_evidence")
                or metrics.get("external_solver_usage_evidence")
            ) or _openhands_usage(openhands_dir)
            if usage_values is not None:
                provider = str(metrics.get("external_solver") or "openhands")
                metrics["usage"] = _append_usage_record(
                    run_dir=run_dir,
                    instance_id=instance_id,
                    model=args.llm_model or args.model_name,
                    usage_values=usage_values,
                    provider=provider,
                    label=f"{provider}-aggregate",
                    status=(
                        "success"
                        if metrics.get("external_solver_evidence") is not None
                        else "technical_failure"
                    ),
                )
            if removed_validation_artifacts:
                metrics["validation_artifacts_removed"] = removed_validation_artifacts
        metrics.update(
            {
                "llm_model": args.llm_model or None,
                "llm_provider": os.environ.get(
                    "OPENCOLLAB_PROVIDER", "anthropic"
                ),
                "llm_base_url_sha256": hashlib.sha256(
                    (
                        os.environ.get("OPENCOLLAB_BASE_URL")
                        or os.environ.get("LLM_BASE_URL")
                        or ""
                    ).encode("utf-8")
                ).hexdigest(),
                "workflow_env": {
                    key: os.environ[key]
                    for key in (
                        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
                        "OPENCOLLAB_TEMPERATURE",
                        "OPENCOLLAB_THINKING",
                        "OPENCOLLAB_THINKING_PARAMS",
                        "OPENCOLLAB_TOP_P",
                    )
                    if key in os.environ
                },
                "context_window": args.context_window,
                "temperature": args.temperature,
                "top_p": args.top_p,
                "max_output_tokens": args.max_output_tokens,
                "runtime_parameter_evidence": {
                    "context_window": {"value": args.context_window, "kind": "configured"},
                    "temperature": {"value": args.temperature, "kind": "configured"},
                    "top_p": {"value": args.top_p, "kind": "configured"},
                    "max_output_tokens": {
                        "value": args.max_output_tokens,
                        "kind": "configured",
                    },
                    "model": {
                        "value": args.llm_model or args.model_name,
                        "kind": (
                            "observed_stream_identity"
                            if isinstance(metrics.get("external_solver_evidence"), dict)
                            else "configured"
                        ),
                    },
                },
                "budget": args.budget,
                "max_steps": args.max_steps,
                "empty_patch_rejections": max(
                    0, args.empty_patch_rejections
                ),
                "openhands_empty_patch_rejections": max(
                    0, args.empty_patch_rejections
                ),
                "openhands_command_sha256": hashlib.sha256(
                    args.command.encode("utf-8")
                ).hexdigest(),
            }
        )
        gp.bind_llm_transport(metrics)
        _complete_openhands_integrity(
            metrics,
            patch=patch,
            snapshot_prepared=snapshot_prepared,
            process_quiesced=process_quiesced,
            patch_extraction_succeeded=patch_extraction_succeeded,
            harness_artifact_exclusion_proven=(
                harness_artifact_exclusion_proven
            ),
        )
        record, metric_record = build_output_records(
            instance_id=instance_id,
            model_name=args.model_name,
            patch=patch,
            metrics=metrics,
            workflow_name="openhands-external",
        )
        pending_required = metrics.get("submission_eligible") is True
        if pending_required:
            pending_path = gp.persist_pending_output(
                run_dir=run_dir,
                predictions_path=Path(args.output),
                metrics_path=Path(args.metrics or f"{args.output}.metrics.jsonl"),
                prediction=record,
                metric=metric_record,
                cid=cid,
                name=name,
            )
    except BaseException as exc:
        if generation_error is None:
            generation_error = exc
        gp.persist_generation_failure(
            run_dir,
            instance_id=instance_id,
            phase="openhands_generation",
            error=generation_error,
        )
        raise
    finally:
        if trusted_baseline is not None:
            trusted_baseline.cleanup()
        try:
            evidence_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(openhands_dir, evidence_dir)
        finally:
            shutil.rmtree(openhands_dir, ignore_errors=True)
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
                completed = (
                    generation_error is None
                    and gp.metrics_have_completed_identity(metrics, patch)
                )
                try:
                    gp.finalize_container_ownership(
                        run_dir=run_dir,
                        cid=cid,
                        name=name,
                        keep_container=(
                            args.keep_container if generation_error is None else False
                        ),
                        completed=completed,
                        metrics=metrics,
                    )
                except BaseException as cleanup_error:
                    if generation_error is None:
                        raise
                    add_note = getattr(generation_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "container cleanup failed after OpenHands generation error: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )

    if generation_error is not None:
        gp.raise_generation_failure_metrics(run_dir, instance_id, "openhands_generation", generation_error, metrics)
    if record is None or metric_record is None:
        raise RuntimeError("OpenHands output record was not built")
    out_path = Path(args.output)
    metrics_path = Path(args.metrics or f"{args.output}.metrics.jsonl")
    if pending_path is not None:
        publish_status = gp.publish_pending_output(run_dir, pending_path)
        gp.require_published_output(
            publish_status,
            label="pending OpenHands output",
        )
    else:
        gp.append_output_records(out_path, metrics_path, record, metric_record)

    if patch.strip():
        print(f"Patch ({len(patch)} chars) written to {out_path}")
    else:
        print("WARNING: empty patch")


if __name__ == "__main__":
    main()
