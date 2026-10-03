"""Execute the production Bash wrappers with real Linux processes and signals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab_eval.generation import terminal_container_backend as backend

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="requires Linux process groups, /proc, GNU env, and setsid"
)


def _python_command(code):
    return "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code)


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def _wait_until(predicate, description, timeout=4):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        await asyncio.sleep(0.01)


def _process_state(pid):
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return SimpleNamespace(state=fields[0], group=int(fields[2]), session=int(fields[3]), birth=fields[19])


def _group_running(group):
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            state = _process_state(int(path.name))
            if state is not None and state.group == group and state.state not in ("Z", "X"):
                return True
    return False


def _ready_code(path, value):
    return (
        f"ready = pathlib.Path({str(path)!r}); "
        "temporary = ready.with_suffix('.tmp'); "
        f"temporary.write_text(json.dumps({value})); temporary.replace(ready)"
    )


@pytest.fixture
async def host_commands(tmp_path, monkeypatch):
    """Replace Docker transport while executing its exact scripts and process groups."""
    if not shutil.which("setsid") or not shutil.which("env"):
        pytest.skip("requires setsid and GNU env")
    probe = subprocess.run(
        ["env", "--default-signal=INT,QUIT", "true"], capture_output=True, check=False
    )
    if probe.returncode:
        pytest.skip("requires GNU env --default-signal support")

    launch = asyncio.create_subprocess_exec
    runner = SimpleNamespace(
        environment=backend.ContainerEnvironment("host-test", str(tmp_path), tmp_path / "commands"),
        executions=[],
        cancellations=[],
        tasks=[],
        groups=set(),
    )
    pidfiles = tmp_path / "pidfiles"
    pidfiles.mkdir()

    async def create(*args, **kwargs):
        assert args[:2] == ("docker", "exec")
        index = args.index("/bin/bash")
        argv = list(args[index:])
        assert argv[1] == "-c"
        expected = {"native-exec": backend.EXEC_WRAPPER, "native-cancel": backend.CANCEL_EXEC}
        assert argv[2] == expected[argv[3]]
        # The host directory represents the container's private /tmp namespace.
        pidfile = pidfiles / Path(argv[4]).name
        argv[4] = str(pidfile)
        process = await launch(*argv, cwd=tmp_path, **kwargs)
        record = SimpleNamespace(process=process, pidfile=pidfile)
        if argv[3] == "native-exec":
            runner.executions.append(record)
        else:
            runner.cancellations.append(record)
        return process

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", create)
    try:
        yield runner
    finally:
        # A failed signal assertion must also stop commands that ignore SIGINT.
        for record in runner.executions:
            if record.pidfile.exists():
                runner.groups.add(int(record.pidfile.read_text()))
        for group in runner.groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        for task in runner.tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        for record in runner.executions + runner.cancellations:
            if record.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    record.process.kill()
            await asyncio.wait_for(record.process.wait(), 5)
            record.pidfile.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_wrapper_preserves_stdin_output_exit_status_and_default_signals(host_commands):
    environment = host_commands.environment
    code = (
        "import json, os, signal, sys; "
        "print(json.dumps({'stdin': sys.stdin.read(), 'pid': os.getpid(), "
        "'group': os.getpgrp(), 'session': os.getsid(0), "
        "'int_default': signal.getsignal(signal.SIGINT) is signal.default_int_handler, "
        "'quit_default': signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL})); "
        "print('command stderr', file=sys.stderr); sys.exit(7)"
    )

    result = await environment.exec_cmd(_python_command(code), stdin=b"input through the wrapper\n", timeout=5)

    output = json.loads(result.stdout)
    assert output["stdin"] == "input through the wrapper\n"
    assert output["int_default"] is True
    assert output["quit_default"] is True
    assert output["pid"] == output["group"] == output["session"]
    assert result.returncode == 7
    assert result.stderr == "command stderr\n"
    assert len(host_commands.executions) == 1
    assert not host_commands.executions[0].pidfile.exists()
    assert not host_commands.cancellations
    await environment.ensure_quiescent()
    assert environment._active == {}
    assert environment._communications == {}
    record = json.loads((environment.artifacts / "commands.jsonl").read_text())
    assert record["returncode"] == 7
    assert record["timed_out"] is False


@pytest.mark.asyncio
async def test_sigint_reaches_python_and_interrupts_the_running_command(tmp_path, host_commands):
    environment = host_commands.environment
    ready = tmp_path / "interrupt-ready.json"
    code = (
        "import json, os, pathlib, signal; print('running', flush=True); "
        + _ready_code(ready, "{'pid': os.getpid(), 'group': os.getpgrp(), 'session': os.getsid(0)}")
        + "; signal.pause()"
    )
    task = asyncio.create_task(environment.exec_cmd(_python_command(code), timeout=None))
    host_commands.tasks.append(task)
    running = await _wait_until(lambda: _read_json(ready), "the interrupt probe to become ready")
    await _wait_until(lambda: environment._active, "command ownership")
    pidfile = host_commands.executions[0].pidfile
    leader = int(pidfile.read_text())
    host_commands.groups.add(leader)
    assert running["pid"] == running["group"] == running["session"] == leader

    os.kill(running["pid"], signal.SIGINT)
    result = await asyncio.wait_for(asyncio.shield(task), 4)

    assert result.returncode == 128 + signal.SIGINT
    assert result.stdout == "running\n"
    assert "KeyboardInterrupt" in result.stderr
    assert not pidfile.exists()
    assert not host_commands.cancellations
    await environment.ensure_quiescent()
    assert environment._active == {}
    assert environment._communications == {}


def _term_ignoring_tree(ready):
    grandchild = (
        "import json, os, pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        + _ready_code(
            ready,
            "{'parent': int(sys.argv[1]), 'child': int(sys.argv[2]), 'grandchild': os.getpid(), "
            "'group': os.getpgrp(), 'session': os.getsid(0)}",
        )
        + "; signal.pause()"
    )
    child = (
        "import os, signal, subprocess, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1], str(os.getpid())]); "
        "signal.pause()"
    )
    parent = (
        "import os, signal, subprocess, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, str(os.getpid())]); signal.pause()"
    )
    return _python_command(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external_cancel", "internal_timeout"])
async def test_cancellation_kills_term_ignoring_process_group_and_retires_command(tmp_path, host_commands, mode):
    environment = host_commands.environment
    ready = tmp_path / "tree-ready.json"
    task = asyncio.create_task(
        environment.exec_cmd(_term_ignoring_tree(ready), timeout=5 if mode == "internal_timeout" else None)
    )
    host_commands.tasks.append(task)
    tree = await _wait_until(lambda: _read_json(ready), "all three generations to become ready")
    await _wait_until(lambda: environment._active, "command ownership")
    pidfile = host_commands.executions[0].pidfile
    leader = int(pidfile.read_text())
    host_commands.groups.add(leader)
    assert tree["parent"] == tree["group"] == tree["session"] == leader
    pids = [tree[name] for name in ("parent", "child", "grandchild")]
    before = {pid: _process_state(pid) for pid in pids}
    for state in before.values():
        assert state is not None
        assert state.group == state.session == leader
        assert state.state not in ("Z", "X")

    if mode == "external_cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 5)
    else:
        result = await asyncio.wait_for(asyncio.shield(task), 7)
        assert result.returncode == 124
        record = json.loads((environment.artifacts / "commands.jsonl").read_text())
        assert record["timed_out"] is True
        assert record["returncode"] == 124

    assert len(host_commands.cancellations) == 1
    assert host_commands.cancellations[0].process.returncode == 0
    assert host_commands.executions[0].process.returncode == 128 + signal.SIGKILL
    await _wait_until(lambda: not _group_running(leader), "the process group to stop")
    for pid in pids:
        after = _process_state(pid)
        assert after is None or after.birth != before[pid].birth or after.state in ("Z", "X")
    assert not pidfile.exists()
    await environment.ensure_quiescent()
    assert environment._active == {}
    assert environment._communications == {}
