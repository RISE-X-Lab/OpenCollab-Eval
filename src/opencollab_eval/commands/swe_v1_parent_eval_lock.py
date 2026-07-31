"""Cross-process locks for eval-only task execution and parent report updates."""

from __future__ import annotations

import argparse
import fcntl
from pathlib import Path
from typing import Any


class ParentEvalLock:
    """Hold one advisory lock below a parent evaluation directory."""

    def __init__(
        self,
        parent_output_dir: Path,
        name: str = "",
        *,
        blocking: bool = True,
    ):
        suffix = f".{name}" if name else ""
        self.path = parent_output_dir.resolve() / f".eval_only{suffix}.lock"
        self.handle: Any | None = None
        self.blocking = blocking

    def __enter__(self) -> ParentEvalLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        operation = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self.handle.fileno(), operation)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"lock is already held: {self.path}") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def parent_eval_lock(args: argparse.Namespace) -> ParentEvalLock:
    """Serialize the same task index while allowing different tasks to evaluate."""
    if not args.eval_only or args.parent_output_dir is None:
        raise RuntimeError("eval-only runs require a parent output directory")
    end_index = args.start_index + max(args.limit, 0) - 1
    return ParentEvalLock(args.parent_output_dir, f"task-{args.start_index}-{end_index}")


def parent_report_lock(args: argparse.Namespace) -> ParentEvalLock:
    """Serialize publication of one parent's cumulative fact report."""
    if not args.eval_only or args.parent_output_dir is None:
        raise RuntimeError("eval-only runs require a parent output directory")
    return ParentEvalLock(args.parent_output_dir, "report")


__all__ = ["ParentEvalLock", "parent_eval_lock", "parent_report_lock"]
