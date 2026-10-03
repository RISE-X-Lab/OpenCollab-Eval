"""Terminal command ownership, delayed exits, and candidate adoption."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import terminal_container_backend as backend


async def _start_host_command(monkeypatch, environment, code):
    """Use real process/pipe lifetimes in place of the Docker transport."""
    launch = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def create(*args, **kwargs):
        process = await launch(
            sys.executable, "-c", code,
            stdin=kwargs.get("stdin"), stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
        )
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(environment.exec_cmd("recorded command", timeout=None))
    await started.wait()
    # Let exec_cmd finish registering the process before its caller cancels it.
    await asyncio.sleep(0)
    return task, processes[0]


@pytest.mark.asyncio
async def test_late_exit_after_failed_cancel_allows_selected_candidate_adoption(tmp_path, monkeypatch):
    environment = backend.ContainerEnvironment("candidate-b", "/app", tmp_path / "commands")

    async def failed_cancel(token, process):
        raise RuntimeError("Docker cancellation transport failed")

    monkeypatch.setattr(environment, "_cancel", failed_cancel)
    task, process = await _start_host_command(
        monkeypatch, environment, "import time; time.sleep(0.2); print('completed')",
    )
    try:
        task.cancel()
        with pytest.raises(RuntimeError, match="cancellation transport"):
            await task

        candidates = backend.ContainerCandidates("source", None, tmp_path / "candidates")
        lease = backend.CandidateLease(candidates, "B", environment)
        lease.captured_diff = "saved complete candidate"
        candidates.leases["B"] = lease
        candidate = SimpleNamespace(label="B", diff=lease.captured_diff)
        with pytest.raises(RuntimeError, match="active foreground"):
            await lease.diff()
        with pytest.raises(RuntimeError, match="active foreground"):
            await lease.cleanup()
        with pytest.raises(RuntimeError, match="foreground"):
            await candidates.adopt_run(candidate)

        await process.wait()
        # communicate() drains the pipes and retires the exact completed command.
        for _ in range(20):
            if not environment._active:
                break
            await asyncio.sleep(0.01)
        monkeypatch.setattr(
            backend, "inspect", lambda _: {"Id": "candidate-b", "State": {"Running": True}},
        )
        await lease.cleanup()
        await candidates.adopt_run(candidate)

        assert candidates.selected is lease
        assert json.loads((tmp_path / "candidates/adoption.json").read_text())["label"] == "B"
        assert environment._active == {}
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_successful_command_retires_and_keeps_actual_exit_and_output(tmp_path, monkeypatch):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path)
    task, _ = await _start_host_command(
        monkeypatch, environment, "import sys; print('answer'); sys.exit(3)",
    )

    result = await task

    assert result.returncode == 3
    assert result.stdout == "answer\n"
    assert not environment._active
    await environment.ensure_quiescent()
    record = json.loads((tmp_path / "commands.jsonl").read_text())
    assert record["returncode"] == 3


@pytest.mark.asyncio
async def test_abort_after_late_exit_does_not_kill_completed_command_again(tmp_path, monkeypatch):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path)

    async def failed_cancel(token, process):
        raise RuntimeError("temporary transport failure")

    monkeypatch.setattr(environment, "_cancel", failed_cancel)
    task, process = await _start_host_command(
        monkeypatch, environment, "import time; time.sleep(0.1)",
    )
    task.cancel()
    with pytest.raises(RuntimeError):
        await task
    await process.wait()
    for _ in range(20):
        if not environment._active:
            break
        await asyncio.sleep(0.01)

    async def unexpected_cancel(token, process):
        pytest.fail("finished command must not be cancelled again")

    monkeypatch.setattr(environment, "_cancel", unexpected_cancel)
    await environment.abort()
    assert environment.revoked
    assert not environment._active


@pytest.mark.asyncio
async def test_pipe_failure_keeps_command_unsettled(tmp_path):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path)
    process = SimpleNamespace(returncode=0)
    communication = asyncio.get_running_loop().create_future()
    communication.set_exception(OSError("lost command output"))
    environment._active["token"] = process
    environment._communications["token"] = communication

    with pytest.raises(RuntimeError, match="active foreground"):
        await environment.ensure_quiescent()
    assert environment._active["token"] is process


@pytest.mark.asyncio
async def test_pending_or_cancelled_output_keeps_command_unsettled(tmp_path):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path)
    process = SimpleNamespace(returncode=0)
    communication = asyncio.get_running_loop().create_future()
    environment._active["token"] = process
    environment._communications["token"] = communication
    with pytest.raises(RuntimeError, match="active foreground"):
        await environment.ensure_quiescent()
    communication.cancel()
    with pytest.raises(RuntimeError, match="active foreground"):
        await environment.ensure_quiescent()


@pytest.mark.asyncio
async def test_late_callback_cannot_retire_another_process(tmp_path):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path)
    previous = SimpleNamespace(returncode=0)
    current = SimpleNamespace(returncode=None)
    communication = asyncio.get_running_loop().create_future()
    communication.set_result((b"", b""))
    environment._active["token"] = current
    environment._communications["token"] = communication

    environment._retire_finished("token", previous)

    assert environment._active["token"] is current


@pytest.mark.asyncio
async def test_adoption_rejects_mismatched_identity_and_stopped_container(tmp_path, monkeypatch):
    environment = backend.ContainerEnvironment("candidate", "/app", tmp_path / "commands")
    candidates = backend.ContainerCandidates("source", None, tmp_path / "candidates")
    lease = backend.CandidateLease(candidates, "B", environment)
    lease.captured_diff = "original"
    candidates.leases["B"] = lease
    with pytest.raises(RuntimeError, match="does not identify"):
        await candidates.adopt_run(SimpleNamespace(label="B", diff="different"))
    monkeypatch.setattr(
        backend, "inspect", lambda _: {"Id": "candidate", "State": {"Running": False}},
    )
    with pytest.raises(RuntimeError, match="not running"):
        await candidates.adopt_run(SimpleNamespace(label="B", diff="original"))
    assert candidates.selected is None
