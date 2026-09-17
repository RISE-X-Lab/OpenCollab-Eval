"""Bind completed-event observations to native workflows and generation controllers."""

from __future__ import annotations

import functools
import math
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import completed_progress_watch as progress

ENV_NAME = "OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT"
WALL_ENV_NAME = "OPENCOLLAB_EVAL_GENERATION_WALL_TIMEOUT"
CONTROLLER_WALL_ENV_NAME = "OPENCOLLAB_EVAL_CONTROLLER_WALL_TIMEOUT"


def timeout_seconds(environment=None):
    value = (os.environ if environment is None else environment).get(ENV_NAME)
    if value is None:
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("no progress timeout must be positive and finite")
    return seconds


def generation_wall_timeout(default, environment=None):
    """Keep legacy wall limits until completed-event supervision is selected."""
    values = os.environ if environment is None else environment
    raw = values.get(WALL_ENV_NAME)
    if raw is not None:
        seconds = float(raw)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("generation wall timeout must be positive and finite")
        return seconds
    return None if timeout_seconds(values) is not None else default


def controller_wall_timeout(default, environment=None):
    values = os.environ if environment is None else environment
    if CONTROLLER_WALL_ENV_NAME in values:
        return generation_wall_timeout(default, {WALL_ENV_NAME: values[CONTROLLER_WALL_ENV_NAME]})
    return None if timeout_seconds(values) is not None else default


def add_arguments(parser):
    parser.add_argument(
        "--no-progress-timeout", type=float,
        help="Stop after this many seconds without actual model or tool progress",
    )
    parser.add_argument("--generation-wall-timeout", type=float, help="Optional cumulative generation limit in seconds")


def configure_arguments(args):
    for field, variable in (("no_progress_timeout", ENV_NAME), ("generation_wall_timeout", WALL_ENV_NAME)):
        value = getattr(args, field, None)
        if value is not None:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"--{field.replace('_', '-')} must be positive and finite")
            os.environ[variable] = str(value)


def config_for(run_dir, *, seconds=None, environment=None):
    values = os.environ if environment is None else environment
    timeout = timeout_seconds(values) if seconds is None else seconds
    config = {
        "output": str(run_dir),
        "run": str(run_dir),
        "native_workflow_events": True,
        "progress_state_path": str(Path(run_dir) / "native-progress-status.json"),
        "native_generator_owner": True,
        "model_progress_sources": [str(Path(run_dir) / "model-progress.jsonl")],
        "no_progress_timeout_seconds": timeout,
        "provider_call_no_progress_timeout_seconds": timeout,
        "progress_poll_seconds": 5,
        "no_progress_cleanup_grace_seconds": 120,
    }
    shared = values.get("OPENCOLLAB_EVAL_MODEL_PROGRESS_PATH")
    binding = values.get("OPENCOLLAB_EVAL_PROGRESS_ID")
    if shared and binding:
        config["model_progress_sources"].append(shared)
        config.update(shared_model_progress_source=shared, model_progress_id=binding)
    return config


def start_epoch(run_dir, default=None):
    owner = progress.read(Path(run_dir) / "native-progress-owner.json", {})
    if isinstance(owner.get("started_epoch"), (int, float)):
        return owner["started_epoch"]
    state = progress.read(Path(run_dir) / "generation.state.json", {})
    try:
        return datetime.fromisoformat(state["last_started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return time.time() if default is None else default


def register_generator(run_dir, *, source=None):
    if timeout_seconds() is None:
        return
    run_dir = Path(run_dir)
    owner_path = run_dir / "native-progress-owner.json"
    owner = progress.read(owner_path, {})
    identity = progress.process_identity(os.getpid())
    if owner.get("generation_identity") != identity or owner.get("generation_completed_epoch") is not None:
        owner = {
            "generation_identity": identity,
            "started_epoch": time.time(),
            "phase": "generating",
            "native_progress_sources": [],
            "progress_id": os.environ.get("OPENCOLLAB_EVAL_PROGRESS_ID"),
        }
    if source is not None and str(source) not in owner["native_progress_sources"]:
        owner["native_progress_sources"].append(str(source))
    progress.save(owner_path, owner)


def finish_generation(run_dir, *, phase="candidate_capture"):
    """Generation ends before candidate capture, report writing and scoring."""
    if timeout_seconds() is None:
        return
    path = Path(run_dir) / "native-progress-owner.json"
    owner = progress.read(path, {})
    owner.update(phase=phase, generation_completed_epoch=time.time())
    progress.save(path, owner)


def generation_timing(run_dir):
    owner = progress.read(Path(run_dir) / "native-progress-owner.json", {})
    if owner.get("generation_identity") != progress.process_identity(os.getpid()):
        return {}
    started, completed = owner.get("started_epoch"), owner.get("generation_completed_epoch")
    if not isinstance(started, (int, float)) or not isinstance(completed, (int, float)):
        return {}
    return {"generation_started_epoch": started, "generation_completed_epoch": completed,
            "generation_duration_seconds": max(0, completed - started),
            "generation_progress_id": owner.get("progress_id"),
            "evaluation_time_policy": "actual_progress_inactivity"}


def record_stage(run_dir, phase):
    path = Path(run_dir) / "native-progress-status.json"
    state = progress.read(path, {})
    if state:
        state.update(phase=phase, stage_started_epoch=time.time())
        progress.save(path, state)


async def guarded_agent(operation, run_dir, artifacts, *, config=None):
    """Use public cancellation cleanup and public journal replay for a paused agent."""
    from opencollab import OpenCollab, RunResult

    register_generator(run_dir, source=Path(artifacts) / "trajectory.jsonl")
    config = config or config_for(run_dir)
    config["native_workflow_sources"] = [str(Path(artifacts) / "trajectory.jsonl")]
    decision = {}
    try:
        return await progress.run_with_stop_request(operation, config, start_epoch(run_dir), decision)
    except progress.EvaluationNoProgressTimeout as error:
        # The public agent coroutine propagates cancellation after its owned
        # execution and final snapshot have quiesced. Lifecycle failures raise.
        path = Path(artifacts) / "agent.json"
        snapshot = OpenCollab.read_session_snapshot(path)
        state = snapshot.get("session_state") or {}
        return RunResult(
            output=None, status="stopped", reason="evaluation_no_progress", error=error,
            artifacts=Path(artifacts),
            tokens=state.get("used_tokens"),
            metrics={"steps": state.get("step_count", 0), "phase": state.get("phase"),
                     "session_quiesced": True, "evaluation_time_policy": "actual_progress_inactivity",
                     "usage_complete": False,
                     "no_progress_timeout": decision, "resume_snapshot": str(path)},
        )
    finally:
        finish_generation(run_dir)


def guarded_workflow(flow, run_dir, *, config=None, orchestration_path=None):
    config = config or config_for(run_dir)
    if orchestration_path is not None:
        register_generator(run_dir, source=orchestration_path)
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
        try:
            return await progress.run_with_stop_request(actual(ctx, args), config, start_epoch(run_dir), decision)
        finally:
            finish_generation(run_dir)

    return guarded


def wait_generation(proc, run_dir, *, wall_timeout, environment=None, config=None):
    seconds = timeout_seconds(environment)
    wall_timeout = generation_wall_timeout(wall_timeout, environment)
    if seconds is None:
        return proc.wait(timeout=wall_timeout)
    config = config or config_for(run_dir, seconds=seconds, environment=environment)
    if wall_timeout is not None:
        config["generation_wall_deadline_epoch"] = time.time() + wall_timeout
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
                        for key in ("progress", "progress_offsets", "native_source_catalog", "provider_fault_waits")
                        if key in saved
                    }
                )
            record = progress.monitor(config, record, status_path, child=proc)
            if record.get("phase") == "paused_for_artifact_recovery":
                # Retain parent ownership for the existing archived recovery procedure.
                while progress.same_process(record["generation_identity"]):
                    time.sleep(interval)
            generation_complete = record.get("generation_completed_epoch") is not None
            remaining = (
                None if wall_timeout is None or generation_complete
                else max(0, config["generation_wall_deadline_epoch"] - time.time())
            )
            return proc.wait(timeout=remaining)
        if wall_timeout is not None and time.time() >= config["generation_wall_deadline_epoch"]:
            raise subprocess.TimeoutExpired(getattr(proc, "args", []), wall_timeout)
        probe.update(phase="preparing", outer_generation_pid=proc.pid)
        progress.save(status_path, probe)
        time.sleep(interval)
    return proc.returncode


def is_progress_stop(native_state):
    return any(
        item.get("type") == "EvaluationNoProgressTimeout"
        and item.get("module") == "opencollab_eval.engine.completed_progress_watch"
        for item in (native_state or {}).get("error_chain", [])
    )
