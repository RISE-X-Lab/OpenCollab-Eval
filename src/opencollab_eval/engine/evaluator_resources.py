from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencollab_eval.engine.async_runtime import (
    abandon_on_timeout,
    force_task_terminal,
    terminate_tasks,
)
from opencollab_eval.engine.environment import ExecutionEnvironment
from opencollab_eval.engine.swe_checkpoint import WorktreeCheckpoint
from opencollab_eval.safe_files import write_regular_bytes_atomic

if TYPE_CHECKING:
    from opencollab_eval.engine.evaluator import EvalTask

WORKFLOW_MANIFEST_FILENAME = "workflow.json"
EnvFactory = Callable[["EvalTask"], Awaitable[ExecutionEnvironment]]
_LATE_EVAL_RESOURCE_TASKS: set[asyncio.Task[Any]] = set()
_LATE_EVAL_RESOURCE_FAILURES: deque[BaseException] = deque(maxlen=64)


async def _wait_for_owned_execution(
    tasks: Sequence[asyncio.Task[Any]],
    workflow_ctx: Any | None,
    *,
    cleanup_timeout: float,
) -> bool:
    """Bound cancellation cleanup before diff extraction and env teardown."""

    def pending_tasks() -> set[asyncio.Task[Any]]:
        pending = {task for task in tasks if not task.done()}
        if workflow_ctx is not None:
            pending.update(getattr(workflow_ctx, "pending_cleanup_tasks", ()))
        return pending

    async def wait_phase(timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        saw_empty = False
        while True:
            pending = pending_tasks()
            if not pending:
                if saw_empty:
                    return True
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _done, still_pending = await asyncio.wait(pending, timeout=remaining)
            if still_pending:
                return False

    if await wait_phase(cleanup_timeout):
        return True

    # A coroutine may consume the first CancelledError while unwinding. A
    # second cancellation interrupts that cleanup wait without making the
    # ordinary deadline depend on an unbounded provider/tool finally block.
    pending = pending_tasks()
    for task in pending:
        task.cancel()
    forced_timeout = min(2.0, max(0.1, cleanup_timeout))
    if await wait_phase(forced_timeout):
        return True
    await terminate_tasks(
        pending_tasks(),
        timeout=forced_timeout,
    )
    # A remaining task stays visible to loop shutdown and makes the evaluation
    # ineligible for submission.
    return False


async def _abort_environment(
    env: ExecutionEnvironment,
    *,
    cleanup_timeout: float,
) -> bool:
    # Revoke the public environment surface synchronously, before an adapter's
    # resource-specific abort hook gets a chance to block or consume cancel.
    env.revoke()
    abort = getattr(env, "abort", None)
    if not callable(abort):
        return True
    task = asyncio.ensure_future(abort())
    if await _wait_for_owned_execution([task], None, cleanup_timeout=cleanup_timeout):
        if task.cancelled():
            return False
        task.result()
        return True
    task.add_done_callback(lambda finished: _consume_background_task(finished))
    return False


async def _cleanup_environment_bounded(
    env: ExecutionEnvironment,
    *,
    cleanup_timeout: float,
) -> bool:
    cleanup_task = asyncio.ensure_future(env.cleanup())
    quiesced = await _wait_for_owned_execution(
        [cleanup_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if cleanup_task.cancelled():
            return False
        cleanup_task.result()
    if not quiesced:
        cleanup_task.add_done_callback(_consume_background_task)
    return quiesced


class _EnvironmentSetupOwner:
    """Keep setup and any late environment under one task's ownership.

    The owner task stays alive until ``run_eval_task`` either accepts the
    environment or relinquishes it.  A factory that consumes cancellation and
    returns after the caller-side deadline is therefore disposed *inside the
    already-owned setup task*.  This matters during ``asyncio.run`` shutdown:
    the runner waits for the original task, while a cleanup task created by a
    done callback would be absent from the runner's cancellation snapshot and
    could be destroyed when the loop closes.

    Each teardown operation runs in a shielded child with a fixed deadline.
    Cancellation aimed at the owner therefore cannot skip disposal, while an
    adapter that never completes becomes explicit disposal evidence instead of
    keeping the event loop alive indefinitely.
    """

    def __init__(
        self,
        factory: EnvFactory,
        eval_task: EvalTask,
        *,
        cleanup_timeout: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._factory = factory
        self._eval_task = eval_task
        self._cleanup_timeout = cleanup_timeout
        self._delivery: asyncio.Future[ExecutionEnvironment] = loop.create_future()
        self._decision = asyncio.Event()
        self._transferred = False
        self._dispose_requested = False
        self._environment: ExecutionEnvironment | None = None
        self.disposal_errors: list[tuple[str, BaseException]] = []
        self.task = loop.create_task(self._run())
        self.task.add_done_callback(_consume_background_task)

    async def acquire(self, timeout: float) -> ExecutionEnvironment:
        return await abandon_on_timeout(self._delivery, timeout)

    def transfer(self, env: ExecutionEnvironment) -> None:
        if env is not self._environment:
            raise RuntimeError("environment setup ownership transfer mismatch")
        if self._dispose_requested:
            raise RuntimeError("environment setup ownership was already relinquished")
        self._transferred = True
        self._decision.set()

    def relinquish(self) -> None:
        self._dispose_requested = True
        self._decision.set()
        if not self.task.done():
            self.task.cancel()

    async def _finish_teardown_operation(
        self,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            operation_task = asyncio.ensure_future(operation())
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))
            return

        waiter = asyncio.create_task(
            asyncio.wait(
                {operation_task},
                timeout=self._cleanup_timeout,
            )
        )
        while True:
            try:
                _done, pending = await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    _done, pending = waiter.result()
                    break
                continue
        if pending:
            operation_task.cancel()
            termination = await force_task_terminal(
                operation_task,
                timeout=self._cleanup_timeout,
            )
            for termination_error in termination.errors:
                self.disposal_errors.append((stage, termination_error))
            self.disposal_errors.append(
                (
                    stage,
                    TimeoutError(f"teardown operation exceeded {self._cleanup_timeout}s"),
                )
            )
            return
        try:
            operation_task.result()
        except BaseException as exc:
            self.disposal_errors.append((stage, exc))

    async def _dispose(self, env: ExecutionEnvironment) -> None:
        # Revoke the adapter synchronously before any teardown await.  A late
        # factory cannot hand a still-active environment to another consumer.
        env.revoke()
        await self._finish_teardown_operation("environment abort failed", env.abort)
        await self._finish_teardown_operation(
            "environment cleanup failed",
            env.cleanup,
        )

    async def _run(self) -> None:
        try:
            env = await self._factory(self._eval_task)
        except asyncio.CancelledError:
            # A factory that propagates cancellation created no transferable
            # environment.  Factories that consume it continue below and their
            # eventual result remains owned here.
            return
        except BaseException as exc:
            if not self._delivery.done():
                self._delivery.set_exception(exc)
            return

        required = ("workspace", "exec_cmd", "revoke", "abort", "cleanup")
        if any(not hasattr(env, name) for name in required):
            if not self._delivery.done():
                self._delivery.set_exception(
                    TypeError("env_factory must return an execution environment")
                )
            return

        self._environment = env
        if self._delivery.done():
            self._dispose_requested = True
        else:
            self._delivery.set_result(env)

        while not self._transferred and not self._dispose_requested:
            try:
                await self._decision.wait()
            except asyncio.CancelledError:
                self._dispose_requested = True

        if self._transferred and not self._dispose_requested:
            return
        await self._dispose(env)


async def _stop_checkpoint_bounded(
    checkpoint: WorktreeCheckpoint,
    env: ExecutionEnvironment,
    *,
    exclude_paths: Sequence[str],
    cleanup_timeout: float,
) -> tuple[bool, Any | None, tuple[asyncio.Task[Any], ...]]:
    stop_task = asyncio.ensure_future(checkpoint.stop(env, exclude_paths=exclude_paths))
    quiesced = await _wait_for_owned_execution(
        [stop_task],
        None,
        cleanup_timeout=cleanup_timeout,
    )
    if quiesced:
        if stop_task.cancelled():
            return False, None, ()
        return True, stop_task.result(), ()
    stop_task.add_done_callback(_consume_background_task)
    return False, None, (stop_task,)


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _workflow_persistence_errors(ctx: Any) -> tuple[Exception, ...]:
    return ()


async def _finalize_eval_workflow_sessions(
    ctx: Any,
    *,
    cleanup_timeout: float,
) -> tuple[bool, tuple[Exception, ...], tuple[asyncio.Task[Any], ...]]:
    """Freeze final session states after execution and await their writers."""
    del cleanup_timeout
    return True, _workflow_persistence_errors(ctx), ()


def _eval_manifest_payload(
    *,
    task: EvalTask,
    workflow: Any,
    ctx: Any,
) -> dict[str, Any]:
    """Freeze values owned by the event-loop before file I/O starts."""
    return {
        "workflow": getattr(workflow, "__name__", "workflow"),
        "task_id": task.task_id,
        "sessions": int(getattr(ctx, "session_count", len(ctx.sessions))),
        "tokens_spent": int(getattr(ctx, "used_tokens", 0)),
        "budget_total": task.max_tokens,
    }


def _write_eval_workflow_manifest(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: Any,
    ctx: Any,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Write ``<run_dir>/workflow.json`` summarising an eval workflow run."""
    if manifest is None:
        manifest = _eval_manifest_payload(
            task=task,
            workflow=workflow,
            ctx=ctx,
        )
    payload = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    write_regular_bytes_atomic(
        Path(run_dir) / WORKFLOW_MANIFEST_FILENAME,
        payload,
        max_bytes=1024 * 1024,
    )


async def _persist_eval_workflow_manifest_owned(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: Any,
    ctx: Any,
    cleanup_timeout: float,
) -> tuple[bool, Exception | None, tuple[asyncio.Task[Any], ...]]:
    manifest = _eval_manifest_payload(
        task=task,
        workflow=workflow,
        ctx=ctx,
    )
    write_task = asyncio.create_task(
        asyncio.to_thread(
            _write_eval_workflow_manifest,
            run_dir,
            task=task,
            workflow=workflow,
            ctx=ctx,
            manifest=manifest,
        )
    )
    done, _pending = await asyncio.wait({write_task}, timeout=cleanup_timeout)
    if write_task not in done:
        write_task.add_done_callback(_consume_background_task)
        return False, None, (write_task,)
    try:
        write_task.result()
    except Exception as exc:
        return True, exc, ()
    return True, None, ()


async def _cleanup_eval_resources_after_tasks(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Any,
    timeout: float,
    env: ExecutionEnvironment | None = None,
) -> None:
    pending = {task for task in dependencies if not task.done()}
    deadline = asyncio.get_running_loop().time() + timeout
    while pending:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        waiter = asyncio.create_task(asyncio.wait(pending, timeout=remaining))
        while True:
            try:
                await asyncio.shield(waiter)
                break
            except asyncio.CancelledError:
                if waiter.done():
                    break
                continue
        _done, pending = waiter.result()
    if pending:
        remaining = max(deadline - asyncio.get_running_loop().time(), 1e-6)
        await terminate_tasks(pending, timeout=remaining)
        _LATE_EVAL_RESOURCE_FAILURES.append(
            TimeoutError(
                "late evaluator resource dependencies did not quiesce; "
                "their environment and tracer remain retained"
            )
        )
        return
    if env is not None:
        remaining = max(deadline - asyncio.get_running_loop().time(), 1e-6)
        try:
            cleaned = await _cleanup_environment_bounded(
                env,
                cleanup_timeout=remaining,
            )
        except BaseException as exc:
            _LATE_EVAL_RESOURCE_FAILURES.append(exc)
            return
        else:
            if not cleaned:
                _LATE_EVAL_RESOURCE_FAILURES.append(
                    TimeoutError(
                        "late evaluator environment cleanup did not quiesce"
                    )
                )
                return
    try:
        tracer.close()
    except BaseException as exc:
        _LATE_EVAL_RESOURCE_FAILURES.append(exc)
        raise


def _late_eval_resource_done(task: asyncio.Task[Any]) -> None:
    _LATE_EVAL_RESOURCE_TASKS.discard(task)
    _consume_background_task(task)


def _defer_eval_resource_cleanup(
    dependencies: Sequence[asyncio.Task[Any]],
    *,
    tracer: Any,
    env: ExecutionEnvironment | None,
) -> None:
    late_timeout = 2.0
    owner = asyncio.create_task(
        _cleanup_eval_resources_after_tasks(
            dependencies,
            tracer=tracer,
            env=env,
            timeout=late_timeout,
        )
    )
    _LATE_EVAL_RESOURCE_TASKS.add(owner)
    owner.add_done_callback(_late_eval_resource_done)
