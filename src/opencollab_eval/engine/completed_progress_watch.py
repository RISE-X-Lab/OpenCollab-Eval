"""Evaluation-side progress timer using existing completed artifact events."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import provider_wait_watchdog


class EvaluationNoProgressTimeout(TimeoutError):
    pass


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + f".{os.getpid()}.pending")
    with pending.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def records(path):
    try:
        with Path(path).open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except FileNotFoundError:
        return


def native_sources(config, state):
    """Read the registered generator trajectory while retaining the observation cursor."""
    if "native_workflow_sources" in config:
        return [(Path(path), "native") for path in config["native_workflow_sources"]]
    catalog = state.setdefault("native_source_catalog", {})
    path = Path(config["output"]) / "native-progress-owner.json"
    try:
        stat = path.stat()
        signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        if catalog.get("signature") != signature:
            owner = read(path, {})
            catalog.update(signature=signature, paths=owner.get("native_progress_sources", []))
    except FileNotFoundError:
        pass
    return [(Path(path), "native") for path in catalog.get("paths", [])]


def latest_progress(config, started_epoch, now=None, state=None):
    """An alive process, a request, an error and file growth are not progress."""
    clock = time.time if now is None else lambda: now
    state = {} if state is None else state
    latest = {"epoch": started_epoch, "kind": "generation_started", "source": None}
    cached = state.get("progress")
    if isinstance(cached, dict) and started_epoch <= cached.get("epoch", 0) <= clock():
        latest = dict(cached)
    offsets = state.setdefault("progress_offsets", {})
    if config.get("native_workflow_events"):
        sources = native_sources(config, state)
    else:
        sources = [(Path(config["output"]) / "model-activity.jsonl", "model")]
        sources.extend((p, "tool") for p in Path(config["run"]).glob("agent-*/trajectory.jsonl"))
    for path, kind in sources:
        for row in incremental_records(path, offsets):
            if kind == "model":
                if row.get("event") != "completed" or not isinstance(row.get("call"), int):
                    continue
                try:
                    end = (
                        float(row["completed_epoch"])
                        if "completed_epoch" in row
                        else (datetime.fromisoformat(row["time"]).timestamp() + float(row["elapsed_seconds"]))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                event_id = row["call"]
            elif kind == "native" and row.get("type") == "llm_call":
                payload = row.get("payload")
                if not isinstance(payload, dict) or not payload.get("model") or "finish_reason" not in payload:
                    continue
                if not {"content", "tool_calls"}.issubset(payload):
                    continue
                end = row.get("timestamp")
                event_id = row.get("step")
            else:
                payload = row.get("payload")
                if row.get("type") != "tool_exec" or not isinstance(payload, dict):
                    continue
                if not payload.get("tool_call_id") or "result" not in payload:
                    continue
                end = row.get("timestamp")
                event_id = payload["tool_call_id"]
            if isinstance(end, (float, int)) and math.isfinite(end) and latest["epoch"] < end <= clock():
                latest = {
                    "epoch": end,
                    "kind": "model_completed" if kind == "model" or row.get("type") == "llm_call" else "tool_completed",
                    "event_id": event_id,
                    "source": str(path),
                }
    return latest


def incremental_records(path, offsets):
    """Persist cursor and progress together in worker-state's existing atomic save."""
    key = str(path)
    try:
        with Path(path).open("rb") as stream:
            stat = os.fstat(stream.fileno())
            cursor = offsets.get(key, {})
            identity = [stat.st_dev, stat.st_ino]
            offset = int(cursor.get("offset", 0)) if cursor.get("identity") == identity else 0
            if offset > stat.st_size:
                offset = 0
            stream.seek(offset)
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    offsets[key] = {"identity": identity, "offset": position}
                    break
                offsets[key] = {"identity": identity, "offset": stream.tell()}
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return


def process_identity(pid):
    """Use OS process birth identity so a reused PID cannot be adopted or signalled."""
    pid = int(pid)
    proc = Path("/proc") / str(pid)
    if Path("/proc/self/stat").exists():
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X"}:
                return None
            argv = (proc / "cmdline").read_text().split("\0")
            if not any(argv):
                return None
            return {"pid": pid, "born": fields[19], "argv": argv}
        except (FileNotFoundError, ProcessLookupError):
            return None
    # Used by the local macOS process simulation. Production uses procfs above.
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    line = result.stdout.strip()
    if result.returncode or not line or line[25:].lstrip().startswith("Z"):
        return None
    return {"pid": pid, "born": line[:24], "argv": line[25:].split(None, 1)[-1]}


def same_process(identity):
    return bool(identity) and process_identity(identity["pid"]) == identity


def observe(config, record, now=None):
    progress = latest_progress(config, record["started_epoch"], now, state=record)
    now = time.time() if now is None else now
    record["progress"] = progress
    record["no_progress_seconds"] = max(0, now - progress["epoch"])
    record["observed_epoch"] = now
    return record["no_progress_seconds"] >= float(config.get("no_progress_timeout_seconds", 43200))


def stop_recheck_state(config, started_epoch):
    """Continue the registered generator observation and recheck its stop condition."""
    path = config.get("progress_state_path")
    saved = read(path, {}) if path else {}
    if saved.get("started_epoch") == started_epoch and saved.get("generation_identity") == process_identity(
        os.getpid()
    ):
        return saved
    return {"started_epoch": started_epoch}


async def run_with_stop_request(operation, config, started_epoch, decision):
    """Cancel cooperatively and let the existing OC lifecycle finalize artifacts."""
    task = asyncio.create_task(operation)
    request_path = Path(config["output"]) / "no-progress-stop.json"
    interval = float(config.get("progress_poll_seconds", 5))
    probe = {"started_epoch": started_epoch}
    role_probe = {}
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            role_sources = [path for path, kind in native_sources(config, role_probe) if kind == "native"]
            role_probe = provider_wait_watchdog.scan(
                role_sources,
                role_probe,
                timeout_seconds=float(
                    config.get(
                        "provider_call_no_progress_timeout_seconds",
                        config.get("no_progress_timeout_seconds", 43200),
                    )
                ),
            )
            save(Path(config["output"]) / "provider-wait-status.json", role_probe)
            if role_probe["pause_candidates"]:
                provider_wait = role_probe["pause_candidates"][0]
                request = {
                    "requested_epoch": time.time(),
                    "reason": "role_model_call_has_no_complete_event",
                    "provider_wait": provider_wait,
                }
                save(Path(config["output"]) / "provider-wait-stop.json", request)
                decision.update(
                    reason="provider_call_no_progress",
                    requested_epoch=request["requested_epoch"],
                    stopped_epoch=time.time(),
                    provider_wait=provider_wait,
                    no_progress_seconds=provider_wait["elapsed_seconds"],
                )
                save(Path(config["output"]) / "no-progress-decision.json", decision)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise EvaluationNoProgressTimeout("Evaluation paused after one role had no complete model event")
            request = read(request_path)
            if request is None:
                continue
            checkpoint = stop_recheck_state(config, started_epoch)
            if checkpoint.get("observed_epoch", -1) > probe.get("observed_epoch", -1):
                probe = checkpoint
            if not observe(config, probe):
                continue
            decision.update(
                reason="evaluation_no_progress",
                requested_epoch=request["requested_epoch"],
                stopped_epoch=time.time(),
                progress=probe["progress"],
                no_progress_seconds=probe["no_progress_seconds"],
            )
            save(Path(config["output"]) / "no-progress-decision.json", decision)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise EvaluationNoProgressTimeout("Evaluation stopped after no completed model or tool event")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def apply_decision(metrics, decision):
    if decision:
        metrics.update(evaluation_time_policy="completed_event_inactivity", no_progress_timeout=decision)
        if metrics.get("session_quiesced") is True and metrics.get("error_type") == "EvaluationNoProgressTimeout":
            metrics.update(
                failure_origin="evaluation_no_progress",
                technical_failure=True,
                oc_failure=False,
                technical_interruption_recoverable=True,
                wall_clock_timeout=False,
            )
    return metrics


def monitor(config, record, state_path, child=None):
    """One owner observes an existing child. A restart continues its original clock."""
    output = Path(config["output"])
    interval = float(config.get("progress_poll_seconds", 5))
    request_path = output / "no-progress-stop.json"
    grace = float(config.get("no_progress_cleanup_grace_seconds", 120))
    while True:
        if child is not None:
            child.poll()
        if not same_process(record["generation_identity"]):
            receipt = read(output / "generation-exit.json")
            if receipt is None and config.get("native_generator_owner"):
                record.update(phase="native_generator_exited", returncode=child.returncode if child else None)
            elif receipt is None:
                record.update(
                    phase="needs_attention",
                    failure_origin="generation_process_exit",
                    artifacts_preserved=True,
                    returncode=child.returncode if child else None,
                )
            else:
                record.update(phase="generation_exited", returncode=receipt["returncode"], generation_exit=receipt)
            save(state_path, record)
            return record
        stalled = observe(config, record)
        request = read(request_path)
        if stalled and request is None:
            request = {
                "requested_epoch": time.time(),
                "progress": record["progress"],
                "reason": "evaluation_no_progress",
            }
            save(request_path, request)
            record["phase"] = "requesting_no_progress_stop"
        elif not stalled and request is not None:
            request_path.unlink(missing_ok=True)
            request = None
            record["phase"] = "resuming"
        if request is not None and time.time() - request["requested_epoch"] >= grace:
            # A blocked interpreter cannot finish OC cleanup. Preserve its memory and
            # workspace for the existing archive path rather than destroy the child.
            if same_process(record["generation_identity"]):
                os.kill(record["generation_identity"]["pid"], signal.SIGSTOP)
            record.update(
                phase="paused_for_artifact_recovery",
                failure_origin="evaluation_no_progress",
                artifacts_preserved=True,
                paused_epoch=time.time(),
            )
            save(state_path, record)
            return record
        save(state_path, record)
        time.sleep(interval)
