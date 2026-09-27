"""Read operator-selected generation capacity between task completions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._swe_g11_config import ParallelConfig, SchedulerState

CAPACITY_CONTROL_FILE_ENV = "OPENCOLLAB_EVAL_CAPACITY_CONTROL_FILE"


def capacity_control_workers(config: ParallelConfig, previous: int) -> int:
    path = os.environ.get(CAPACITY_CONTROL_FILE_ENV, "").strip()
    if not path:
        return previous
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        workers = document["generation_workers"]
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            return previous
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return previous
    return min(config.max_workers, workers)


def refresh_capacity_control(config: ParallelConfig, scheduler: SchedulerState) -> None:
    workers = capacity_control_workers(config, scheduler.current_workers)
    if workers == scheduler.current_workers:
        return
    previous = scheduler.current_workers
    scheduler.current_workers = workers
    scheduler.events.append(
        {
            "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "action": "capacity_control_update",
            "previous_workers": previous,
            "new_workers": workers,
            "source": os.environ.get(CAPACITY_CONTROL_FILE_ENV, ""),
        }
    )


def wait_timeout() -> float | None:
    """Wake the task scheduler while a capacity control file is configured."""
    return 1.0 if os.environ.get(CAPACITY_CONTROL_FILE_ENV, "").strip() else None
