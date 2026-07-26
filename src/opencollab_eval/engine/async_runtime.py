"""Bounded async ownership helpers used by evaluation-controlled operations."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


def add_exception_note(error: BaseException, note: str) -> bool:
    """Attach one diagnostic note on Python 3.10 and newer."""
    if not isinstance(note, str):
        raise TypeError("exception note must be text")
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)
        return True
    notes = getattr(error, "__notes__", None)
    if notes is None:
        notes = []
        try:
            error.__notes__ = notes
        except (AttributeError, TypeError):
            return False
    append = getattr(notes, "append", None)
    if not callable(append):
        return False
    append(note)
    return True


async def await_owned_operation(
    awaitable: Awaitable[T],
    *,
    propagate_cancellation: bool = False,
) -> T:
    """Keep an owned operation alive through repeated caller cancellation."""
    owner = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(owner)
            break
        except asyncio.CancelledError as exc:
            if owner.done() and owner.cancelled():
                raise
            cancellation = cancellation or exc
        except BaseException as exc:
            if cancellation is not None and propagate_cancellation:
                add_exception_note(
                    cancellation,
                    f"owned operation also failed: {type(exc).__name__}: {exc}",
                )
                raise cancellation from exc
            raise
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return result


class CallerTimeoutError(asyncio.TimeoutError):
    """A caller-owned wall-clock deadline expired."""


class AsyncRuntimeUnhealthyError(RuntimeError):
    """Pending async work missed a required shutdown deadline."""


@dataclass(frozen=True)
class TaskTerminationResult:
    terminal: bool
    cancellation: asyncio.CancelledError | None
    errors: tuple[BaseException, ...]


async def force_task_terminal(
    task: asyncio.Future[object],
    *,
    timeout: float = 0.1,
    cancellation: asyncio.CancelledError | None = None,
) -> TaskTerminationResult:
    """Cancel one owner and report whether it became terminal."""
    if isinstance(timeout, bool):
        raise ValueError("task termination timeout must be finite and positive")
    try:
        phase_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "task termination timeout must be finite and positive"
        ) from exc
    if not math.isfinite(phase_timeout) or phase_timeout <= 0:
        raise ValueError("task termination timeout must be finite and positive")
    if task is asyncio.current_task():
        raise RuntimeError("cannot force the current task to terminate")
    errors: list[BaseException] = []
    if not task.done():
        task.cancel()
        deadline = asyncio.get_running_loop().time() + phase_timeout
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            errors.append(exc)
    else:
        errors.append(
            TimeoutError(
                "async owner did not reach a terminal state before the cleanup deadline"
            )
        )
    return TaskTerminationResult(task.done(), cancellation, tuple(errors))


async def terminate_tasks(
    tasks: Iterable[asyncio.Future[object]],
    *,
    timeout: float,
) -> tuple[TaskTerminationResult, ...]:
    """Cancel unique pending tasks and return their terminal evidence."""
    unique = tuple(dict.fromkeys(task for task in tasks if not task.done()))
    if not unique:
        return ()
    return tuple(
        await asyncio.gather(
            *(force_task_terminal(task, timeout=timeout) for task in unique)
        )
    )


def _consume_task_result(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def abandon_on_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    *,
    timeout_error_type: type[CallerTimeoutError] = CallerTimeoutError,
    task_tracker: Callable[[asyncio.Task[T]], None] | None = None,
) -> T:
    """Cancel an awaitable at the caller deadline without awaiting its cleanup."""
    if timeout is None:
        return await awaitable
    if isinstance(timeout, bool):
        raise ValueError("timeout must be a finite positive number or None")
    try:
        normalized = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite positive number or None") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("timeout must be a finite positive number or None")
    task = asyncio.ensure_future(awaitable)
    if task_tracker is not None and isinstance(task, asyncio.Task):
        task_tracker(task)
    try:
        done, _pending = await asyncio.wait({task}, timeout=normalized)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_consume_task_result)
    raise timeout_error_type


def run_with_bounded_shutdown(
    awaitable: Awaitable[T],
    *,
    shutdown_timeout: float = 2.0,
) -> T:
    """Run one CLI coroutine and require pending tasks to stop on exit."""
    if isinstance(shutdown_timeout, bool):
        raise ValueError("shutdown_timeout must be a finite positive number")
    try:
        timeout = float(shutdown_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("shutdown_timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("shutdown_timeout must be a finite positive number")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run_with_bounded_shutdown cannot run inside an event loop")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run_error: BaseException | None = None
    result: T | None = None
    try:
        try:
            result = loop.run_until_complete(awaitable)
        except BaseException as exc:
            run_error = exc
        deadline = loop.time() + timeout
        observed: set[asyncio.Task[object]] = set()
        while True:
            pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
            observed.update(pending)
            if not pending or loop.time() >= deadline:
                break
            for task in pending:
                task.cancel()
            loop.run_until_complete(
                asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
            )
        lingering = {task for task in asyncio.all_tasks(loop) if not task.done()}
        for task in observed:
            _consume_task_result(task)
        if run_error is not None:
            raise run_error
        if lingering:
            raise AsyncRuntimeUnhealthyError(
                f"{len(lingering)} async task(s) missed the shutdown deadline"
            )
        return result  # type: ignore[return-value]
    finally:
        loop.close()
        asyncio.set_event_loop(None)


__all__ = [
    "AsyncRuntimeUnhealthyError",
    "CallerTimeoutError",
    "TaskTerminationResult",
    "abandon_on_timeout",
    "add_exception_note",
    "await_owned_operation",
    "force_task_terminal",
    "run_with_bounded_shutdown",
    "terminate_tasks",
]
