"""Bind completed-event observations to native workflows and generation controllers."""

from __future__ import annotations

import functools
import math
import os
import time
from datetime import datetime
from pathlib import Path

from . import completed_progress_watch as progress

ENV_NAME = "OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT"


def timeout_seconds(environment=None):
    value = (os.environ if environment is None else environment).get(ENV_NAME)
    if value is None:
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("no progress timeout must be positive and finite")
    return seconds


def config_for(run_dir, *, seconds=None):
    timeout = timeout_seconds() if seconds is None else seconds
    return {
        "output": str(run_dir),
        "run": str(run_dir),
        "native_workflow_events": True,
        "progress_state_path": str(Path(run_dir) / "native-progress-status.json"),
        "native_generator_owner": True,
        "no_progress_timeout_seconds": timeout,
        "provider_call_no_progress_timeout_seconds": timeout,
        "progress_poll_seconds": 5,
        "no_progress_cleanup_grace_seconds": 120,
    }


def start_epoch(run_dir, default=None):
    state = progress.read(Path(run_dir) / "generation.state.json", {})
    try:
        return datetime.fromisoformat(state["last_started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return time.time() if default is None else default


def register_generator(run_dir):
    if timeout_seconds() is None:
        return
    run_dir = Path(run_dir)
    progress.save(
        run_dir / "native-progress-owner.json",
        {
            "generation_identity": progress.process_identity(os.getpid()),
            "started_epoch": start_epoch(run_dir),
            "phase": "generating",
            "native_progress_sources": [],
        },
    )


def guarded_workflow(flow, run_dir, *, config=None, orchestration_path=None):
    config = config or config_for(run_dir)
    if orchestration_path is not None:
        owner_path = Path(run_dir) / "native-progress-owner.json"
        owner = progress.read(owner_path, {})
        paths = owner.setdefault("native_progress_sources", [])
        if str(orchestration_path) not in paths:
            paths.append(str(orchestration_path))
        progress.save(owner_path, owner)
        config["native_workflow_sources"] = paths
    actual = getattr(flow, "fn", flow)

    @functools.wraps(actual)
    async def guarded(ctx, args):
        decision = {}
        return await progress.run_with_stop_request(actual(ctx, args), config, start_epoch(run_dir), decision)

    return guarded


def wait_generation(proc, run_dir, *, wall_timeout, environment=None, config=None):
    seconds = timeout_seconds(environment)
    if seconds is None:
        return proc.wait(timeout=wall_timeout)
    config = config or config_for(run_dir, seconds=seconds)
    run_dir = Path(run_dir)
    owner_path = run_dir / "native-progress-owner.json"
    status_path = run_dir / "native-progress-status.json"
    started = start_epoch(run_dir)
    interval = config["progress_poll_seconds"]
    probe = {"started_epoch": started}
    while proc.poll() is None:
        owner = progress.read(owner_path)
        if owner and progress.same_process(owner.get("generation_identity")):
            record = {**owner, "outer_generation_pid": proc.pid}
            saved = progress.read(status_path, {})
            if (
                saved.get("generation_identity") == owner["generation_identity"]
                and saved.get("started_epoch") == owner["started_epoch"]
            ):
                record.update(
                    {
                        key: saved[key]
                        for key in ("progress", "progress_offsets", "native_source_catalog")
                        if key in saved
                    }
                )
            record = progress.monitor(config, record, status_path, child=proc)
            if record.get("phase") == "paused_for_artifact_recovery":
                # Retain parent ownership for the existing archived recovery procedure.
                while progress.same_process(record["generation_identity"]):
                    time.sleep(interval)
            return proc.wait(timeout=wall_timeout)
        if progress.observe(config, probe):
            progress.save(
                status_path,
                {
                    **probe,
                    "phase": "waiting_for_generation_owner",
                    "outer_generation_pid": proc.pid,
                    "artifacts_preserved": True,
                },
            )
        time.sleep(interval)
    return proc.returncode


def is_progress_stop(native_state):
    return any(
        item.get("type") == "EvaluationNoProgressTimeout"
        and item.get("module") == "opencollab_eval.engine.completed_progress_watch"
        for item in (native_state or {}).get("error_chain", [])
    )
