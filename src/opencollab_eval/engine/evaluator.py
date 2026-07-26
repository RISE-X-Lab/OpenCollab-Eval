"""Headless Evaluation Runner — for SWE-bench and research benchmarks.

Provides a pure, non-interactive entry point for batch evaluation.
Each task runs in an isolated environment, produces a git patch, and
records a full trajectory for analysis.

Ref:
- Design doc: run_eval_task with Environment + issue_desc → patch
- Harness Engineering: standardized output, sandboxed execution, trajectory recording
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
import shlex as shlex
import stat
import time as time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass as dataclass
from pathlib import Path, PureWindowsPath
from typing import Any as Any

from opencollab.environments import (
    docker_environment,
    worktree_environment,
)
from opencollab.tools import Tool, builtin_tools

from opencollab_eval.engine.async_runtime import (
    add_exception_note,
    await_owned_operation,
)
from opencollab_eval.engine.environment import ExecutionEnvironment
from opencollab_eval.engine.evaluator_models import EvalResult as EvalResult
from opencollab_eval.engine.evaluator_models import EvalTask as EvalTask
from opencollab_eval.engine.evaluator_resources import (
    _LATE_EVAL_RESOURCE_FAILURES as _LATE_EVAL_RESOURCE_FAILURES,
)
from opencollab_eval.engine.evaluator_resources import (
    _LATE_EVAL_RESOURCE_TASKS as _LATE_EVAL_RESOURCE_TASKS,
)
from opencollab_eval.engine.evaluator_resources import (
    _abort_environment as _abort_environment,
)
from opencollab_eval.engine.evaluator_resources import (
    _cleanup_environment_bounded as _cleanup_environment_bounded,
)
from opencollab_eval.engine.evaluator_resources import (
    _cleanup_eval_resources_after_tasks as _cleanup_eval_resources_after_tasks,
)
from opencollab_eval.engine.evaluator_resources import (
    _consume_background_task as _consume_background_task,
)
from opencollab_eval.engine.evaluator_resources import (
    _defer_eval_resource_cleanup as _defer_eval_resource_cleanup,
)
from opencollab_eval.engine.evaluator_resources import (
    _EnvironmentSetupOwner as _EnvironmentSetupOwner,
)
from opencollab_eval.engine.evaluator_resources import (
    _eval_manifest_payload as _eval_manifest_payload,
)
from opencollab_eval.engine.evaluator_resources import (
    _finalize_eval_workflow_sessions as _finalize_eval_workflow_sessions,
)
from opencollab_eval.engine.evaluator_resources import (
    _late_eval_resource_done as _late_eval_resource_done,
)
from opencollab_eval.engine.evaluator_resources import (
    _persist_eval_workflow_manifest_owned as _persist_eval_workflow_manifest_owned,
)
from opencollab_eval.engine.evaluator_resources import (
    _stop_checkpoint_bounded as _stop_checkpoint_bounded,
)
from opencollab_eval.engine.evaluator_resources import (
    _wait_for_owned_execution as _wait_for_owned_execution,
)
from opencollab_eval.engine.evaluator_resources import (
    _workflow_persistence_errors as _workflow_persistence_errors,
)
from opencollab_eval.engine.evaluator_resources import (
    _write_eval_workflow_manifest as _write_eval_workflow_manifest,
)
from opencollab_eval.engine.evaluator_sessions import (
    _aggregate_markup_recovery as _aggregate_markup_recovery,
)
from opencollab_eval.engine.evaluator_sessions import (
    _aggregate_steps as _aggregate_steps,
)
from opencollab_eval.engine.evaluator_sessions import (
    _aggregate_tokens as _aggregate_tokens,
)
from opencollab_eval.engine.evaluator_sessions import (
    _run_single_session as _run_single_session,
)
from opencollab_eval.engine.evaluator_sessions import (
    _run_workflow_mode as _run_workflow_mode,
)
from opencollab_eval.engine.evaluator_task import run_eval_task_impl
from opencollab_eval.engine.evidence_trace import EvidenceTrace
from opencollab_eval.engine.swe_checkpoint import (
    WorktreeCheckpoint as WorktreeCheckpoint,
)
from opencollab_eval.engine.swe_checkpoint import (
    worktree_diff_command as worktree_diff_command,
)
from opencollab_eval.engine.swe_checkpoint_artifacts import (
    workspace_relative_host_paths,
)
from opencollab_eval.engine.test_injection import (
    TestPatchIsolationError as TestPatchIsolationError,
)
from opencollab_eval.engine.test_injection import (
    apply_test_patch as apply_test_patch,
)
from opencollab_eval.safe_files import (
    ensure_directory_no_symlinks,
    open_directory_no_symlinks,
    write_regular_file_atomic,
)
from opencollab_eval.usage import DEFAULT_MAX_OUTPUT_TOKENS

DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P: float | None = None
DEFAULT_THINKING = False
DEFAULT_THINKING_PARAMS = {"enable_thinking": True}
ORCHESTRATION_FILENAME = "orchestration.jsonl"
Tracer = EvidenceTrace
DockerEnvironment = docker_environment
WorktreeEnvironment = worktree_environment

EnvFactory = Callable[["EvalTask"], Awaitable[ExecutionEnvironment]]
ToolFactory = Callable[[], Sequence[Tool]]
WorkflowFn = Callable[..., Awaitable[Any]]


EVAL_AGENT_PROMPT = """\
You are an autonomous coding agent. Complete the following task by modifying the code.

Rules:
- Read relevant files before making changes.
- Make minimal, targeted changes to fix the issue.
- After making changes, verify them (run tests if available).
- When done, make sure all changes are saved. Do NOT commit.
"""

DEFAULT_MAX_STEPS = 80
DEFAULT_EXECUTION_CLEANUP_TIMEOUT = 10.0
MAX_TASK_ID_BYTES = 240
RESULT_TEMP_DIRECTORY = ".opencollab-results-tmp"
MAX_LEGACY_RESULT_TEMP_ARTIFACTS = 256
MAX_RESULT_RECORD_BYTES = 64 * 1024 * 1024
MAX_RESULTS_FILE_BYTES = 512 * 1024 * 1024
MAX_TASK_HARNESS_ARTIFACT_PATHS = 256
MAX_TASK_HARNESS_ARTIFACT_PATH_BYTES = 32 * 1024
MAX_MAPPED_HARNESS_ARTIFACT_PATHS = 520
MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES = 128 * 1024


def _append_harness_error(current: str | None, stage: str, exc: Exception) -> str:
    detail = f"{stage}: {type(exc).__name__}: {exc}"
    return f"{current}; {detail}" if current else detail


def _validate_task_id(task_id: object) -> str:
    if not isinstance(task_id, str) or not task_id or task_id in {".", ".."}:
        raise ValueError("task_id must be a non-empty path-safe string")
    try:
        encoded_task_id = os.fsencode(task_id)
    except UnicodeEncodeError as exc:
        raise ValueError("task_id must be a non-empty path-safe string") from exc
    if (
        os.path.isabs(task_id)
        or PureWindowsPath(task_id).drive
        or "/" in task_id
        or "\\" in task_id
        or any(ord(char) < 32 or ord(char) == 127 for char in task_id)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in task_id)
        or len(encoded_task_id) > MAX_TASK_ID_BYTES
    ):
        raise ValueError("task_id must be a non-empty path-safe string")
    return task_id


def _task_id_collision_key(task_id: str) -> str:
    return unicodedata.normalize("NFC", task_id).casefold()


def _validate_harness_artifact_paths(paths: object) -> tuple[str, ...]:
    if not isinstance(paths, tuple):
        raise ValueError("harness_artifact_paths must be a tuple of non-empty strings")
    if len(paths) > MAX_TASK_HARNESS_ARTIFACT_PATHS:
        raise ValueError(
            "harness_artifact_paths exceeds the path-count safety bound"
        )
    normalized: list[str] = []
    total_bytes = 0
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or "\0" in path
            or any(0xD800 <= ord(char) <= 0xDFFF for char in path)
        ):
            raise ValueError(
                "harness_artifact_paths must contain filesystem-safe strings"
            )
        try:
            encoded = os.fsencode(path)
        except UnicodeEncodeError as exc:
            raise ValueError(
                "harness_artifact_paths must contain filesystem-safe strings"
            ) from exc
        total_bytes += len(encoded)
        if total_bytes > MAX_TASK_HARNESS_ARTIFACT_PATH_BYTES:
            raise ValueError(
                "harness_artifact_paths exceeds the aggregate-byte safety bound"
            )
        normalized.append(path)
    return tuple(normalized)


def _mapped_artifact_path_bound_error(paths: Sequence[str]) -> str | None:
    if len(paths) > MAX_MAPPED_HARNESS_ARTIFACT_PATHS:
        return (
            f"mapped artifact path count {len(paths)} exceeds "
            f"{MAX_MAPPED_HARNESS_ARTIFACT_PATHS}"
        )
    total_bytes = sum(
        len(path.encode("utf-8", errors="surrogatepass")) for path in paths
    )
    if total_bytes > MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES:
        return (
            f"mapped artifact path bytes {total_bytes} exceed "
            f"{MAX_MAPPED_HARNESS_ARTIFACT_PATH_BYTES}"
        )
    return None


def _host_workspace_root(env: ExecutionEnvironment) -> Path | None:
    raw_workspace = (
        env.workspace
        if env.local_filesystem
        else getattr(env, "host_workspace", None)
    )
    if not raw_workspace:
        return None
    try:
        return Path(os.path.abspath(os.fspath(raw_workspace)))
    except (OSError, TypeError, ValueError):
        return None


def _workspace_relative_host_paths(
    env: ExecutionEnvironment,
    raw_path: str | os.PathLike[str],
) -> list[Path]:
    return list(workspace_relative_host_paths(env, raw_path))


def _workspace_relative_host_path(
    env: ExecutionEnvironment,
    raw_path: str | os.PathLike[str],
) -> Path | None:
    paths = _workspace_relative_host_paths(env, raw_path)
    return paths[0] if paths else None


def _workspace_relative_artifact_paths(
    env: ExecutionEnvironment,
    paths: Sequence[str | os.PathLike[str]],
) -> list[str]:
    relative_paths: list[Path] = []
    for raw_path in paths:
        for relative in _workspace_relative_host_paths(env, raw_path):
            if relative == Path("."):
                continue
            if relative not in relative_paths:
                relative_paths.append(relative)

    selected: list[Path] = []
    for relative in sorted(relative_paths, key=lambda path: len(path.parts)):
        if any(parent == relative or parent in relative.parents for parent in selected):
            continue
        selected.append(relative)
    return [path.as_posix() for path in selected]


def _legacy_result_temp_paths(output_dir: str) -> tuple[list[str], bool]:
    matches: list[str] = []
    try:
        with os.scandir(output_dir) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(".results.jsonl.")
                    and entry.name.endswith(".tmp")
                ):
                    continue
                if len(matches) >= MAX_LEGACY_RESULT_TEMP_ARTIFACTS:
                    return matches, False
                matches.append(entry.path)
    except OSError:
        return matches, False
    return matches, True


def default_tools() -> list[Tool]:
    """Build the default tool set for an eval agent.

    Mirrors the curated single-agent surface used by team roles (coder +
    reviewer tools) so headless eval exercises the same toolset: the bash
    description deflects to run_tests/git_diff/grep, and apply_patch is the
    fallback when str_replace edits fail to match.
    """
    return list(
        builtin_tools(
            "bash",
            "file_read",
            "file_write",
            "apply_patch",
            "run_tests",
            "git_diff",
            "grep",
            headless=True,
        )
    )


async def build_repository_map(env: ExecutionEnvironment) -> str:
    """Build a bounded file list through the public environment contract."""
    result = await env.exec_cmd(
        "git -c core.quotepath=false ls-files -z",
        timeout=30.0,
    )
    if result.returncode != 0 or result.stdout_truncated:
        return ""
    paths = [path for path in result.stdout.split("\0") if path]
    if not paths:
        return ""
    selected = paths[:400]
    suffix = "\n..." if len(paths) > len(selected) else ""
    return "Repository files\n" + "\n".join(selected) + suffix


async def default_env_factory(task: EvalTask) -> ExecutionEnvironment:
    """Build the default environment for a task (docker if imaged, else local)."""
    if task.docker_image:
        backing: ExecutionEnvironment | None = None
        if task.repo_path:
            backing = WorktreeEnvironment(task.repo_path)
        env = DockerEnvironment(
            image=task.docker_image,
            backing_environment=backing,
        )
        try:
            mount_dir = None
            if backing is not None:
                mount_dir = await backing.setup()
            await env.setup(mount_dir=mount_dir)
            return env
        except BaseException as original:
            try:
                await await_owned_operation(env.cleanup())
            except BaseException as cleanup_exc:
                add_exception_note(
                    original,
                    "isolated Docker environment cleanup failed: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                )
            raise original
    env = WorktreeEnvironment(task.repo_path or ".")
    await env.setup()
    return env


async def run_eval_task(
    task: EvalTask,
    model: str = "gpt-4o",
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    output_dir: str = "eval_results",
    prompt: str = EVAL_AGENT_PROMPT,
    tools_factory: ToolFactory = default_tools,
    env_factory: EnvFactory = default_env_factory,
    max_steps: int = DEFAULT_MAX_STEPS,
    workflow: WorkflowFn | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    checkpoint_interval_seconds: float | None = None,
    resume_from_checkpoint: bool = False,
    cancellation_cleanup_timeout: float = DEFAULT_EXECUTION_CLEANUP_TIMEOUT,
    defer_patch_extraction: bool = False,
) -> EvalResult:
    """Run one isolated evaluation task in session or workflow mode."""
    return await run_eval_task_impl(
        task,
        model,
        provider,
        api_key,
        base_url,
        output_dir,
        prompt,
        tools_factory,
        env_factory,
        max_steps,
        workflow,
        temperature,
        top_p,
        max_output_tokens,
        thinking,
        thinking_params,
        checkpoint_interval_seconds,
        resume_from_checkpoint,
        cancellation_cleanup_timeout,
        defer_patch_extraction,
    )


async def _cancel_and_wait_eval_workers(
    workers: list[asyncio.Task[EvalResult]],
) -> None:
    for worker in workers:
        if not worker.done():
            worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


async def run_eval_batch(
    tasks: list[EvalTask],
    concurrency: int = 4,
    **kwargs,
) -> list[EvalResult]:
    """Run multiple evaluation tasks with controlled concurrency.

    Individual task failures produce an EvalResult with error set,
    rather than aborting the entire batch.
    """
    if isinstance(concurrency, bool):
        raise ValueError("concurrency must be a positive integer")
    try:
        concurrency = operator.index(concurrency)
    except TypeError as exc:
        raise ValueError("concurrency must be a positive integer") from exc
    if concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    task_ids = [_validate_task_id(task.task_id) for task in tasks]
    collision_keys = [_task_id_collision_key(task_id) for task_id in task_ids]
    if len(set(collision_keys)) != len(collision_keys):
        raise ValueError("task_id values must be unique within an evaluation batch")
    semaphore = asyncio.Semaphore(concurrency)
    workers: list[asyncio.Task[EvalResult]] = []

    def integrity_unknown_result(task: EvalTask, error: str) -> EvalResult:
        return EvalResult(
            task_id=task.task_id,
            patch="",
            patch_produced=False,
            tokens_used=0,
            steps=0,
            duration=0.0,
            error=error,
            execution_quiesced=False,
            patch_extraction_succeeded=False,
            injected_path_cleanup_proven=False,
            harness_artifact_exclusion_proven=False,
            checkpoint_restore_integrity_proven=False,
            task_stage_integrity_proven=False,
            submission_eligible=False,
        )

    async def run_one(task: EvalTask) -> EvalResult:
        async with semaphore:
            try:
                return await run_eval_task(task, **kwargs)
            except Exception as exc:
                return integrity_unknown_result(
                    task,
                    f"Unhandled: {type(exc).__name__}: {exc}",
                )

    workers = [
        asyncio.create_task(run_one(task))
        for task in tasks
    ]
    try:
        return await asyncio.gather(*workers)
    except BaseException:
        await _cancel_and_wait_eval_workers(workers)
        raise


def save_results(results: list[EvalResult], output_path: str) -> None:
    """Durably save evaluation results and their recoverable patches as JSONL."""
    target = Path(os.path.abspath(output_path))
    if not target.name or target.name in {".", ".."}:
        raise ValueError("output_path must name a results file")
    ensure_directory_no_symlinks(target.parent)
    parent_fd = open_directory_no_symlinks(target.parent)
    try:
        parent = os.fstat(parent_fd)
        expected_parent_identity = (parent.st_dev, parent.st_ino)
        try:
            existing = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"results target is not a regular file: {target}")
    finally:
        os.close(parent_fd)

    def write_jsonl(handle) -> None:
        total_bytes = 0
        for result in results:
            record = {
                "task_id": result.task_id,
                "patch": result.patch,
                "patch_produced": result.patch_produced,
                "tokens_used": result.tokens_used,
                "steps": result.steps,
                "duration": round(result.duration, 2),
                "error": result.error,
                "patch_lines": len(result.patch.splitlines()),
                "trajectory": result.trajectory_path,
                "test_patch_isolation_failed": result.test_patch_isolation_failed,
                "execution_quiesced": result.execution_quiesced,
                "patch_extraction_succeeded": result.patch_extraction_succeeded,
                "injected_path_cleanup_proven": (
                    result.injected_path_cleanup_proven
                ),
                "harness_artifact_exclusion_proven": (
                    result.harness_artifact_exclusion_proven
                ),
                "checkpoint_restore_integrity_proven": (
                    result.checkpoint_restore_integrity_proven
                ),
                "task_stage_integrity_proven": (
                    result.task_stage_integrity_proven
                ),
                "submission_eligible": result.submission_eligible,
            }
            if result.checkpoint_result is not None:
                record["checkpoint_result"] = result.checkpoint_result
            line = (json.dumps(record) + "\n").encode("utf-8")
            if len(line) > MAX_RESULT_RECORD_BYTES:
                raise ValueError(
                    f"evaluation result record exceeds {MAX_RESULT_RECORD_BYTES} bytes"
                )
            total_bytes += len(line)
            if total_bytes > MAX_RESULTS_FILE_BYTES:
                raise ValueError(
                    f"evaluation results exceed {MAX_RESULTS_FILE_BYTES} bytes"
                )
            written = handle.write(line)
            if written != len(line):
                raise OSError("evaluation results write made no progress")

    write_regular_file_atomic(
        target,
        write_jsonl,
        max_bytes=MAX_RESULTS_FILE_BYTES,
        expected_parent_identity=expected_parent_identity,
        expected_target_identity=(
            (existing.st_dev, existing.st_ino) if existing is not None else None
        ),
        require_target_absent=existing is None,
        context="evaluation results",
    )
