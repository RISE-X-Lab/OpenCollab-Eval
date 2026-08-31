from __future__ import annotations

import json
import subprocess

from opencollab_eval.generation import external_solver_containers as esc

TASK_ID = "solver-" + "1" * 32
CID = "a" * 64
NETWORK = f"oc-claude-net-{TASK_ID}"


def _completed(command: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_container_inspect_retries_transient_daemon_failure(monkeypatch) -> None:
    inspect_calls = 0
    state = {CID}

    def docker(command: list[str], **_kwargs: object):
        nonlocal inspect_calls
        if command[:2] == ["docker", "inspect"]:
            inspect_calls += 1
            if inspect_calls == 1:
                return _completed(command, 1, stderr="Cannot connect to the Docker daemon")
            if CID in state:
                return _completed(
                    command,
                    0,
                    json.dumps(
                        {
                            "opencollab.owner": "claude-code-external",
                            "opencollab.solver_task_id": TASK_ID,
                        }
                    ),
                )
            return _completed(command, 1, stderr="Error: No such object")
        if command[:3] == ["docker", "rm", "-f"]:
            state.remove(CID)
            return _completed(command, 0, CID + "\n")
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)
    cleaned, record = esc._remove_container(
        CID,
        solver_task_id=TASK_ID,
        expected_owner="claude-code-external",
        cidfile="test-container.id",
    )

    assert cleaned is True
    assert record["status"] == "removed"
    assert inspect_calls == 3  # one transient retry, then the post-remove proof


def test_container_remove_retries_transient_daemon_failure(monkeypatch) -> None:
    remove_calls = 0
    state = {CID}

    def docker(command: list[str], **_kwargs: object):
        nonlocal remove_calls
        if command[:2] == ["docker", "inspect"]:
            if CID in state:
                return _completed(
                    command,
                    0,
                    json.dumps(
                        {
                            "opencollab.owner": "claude-code-external",
                            "opencollab.solver_task_id": TASK_ID,
                        }
                    ),
                )
            return _completed(command, 1, stderr="Error: No such object")
        if command[:3] == ["docker", "rm", "-f"]:
            remove_calls += 1
            if remove_calls == 1:
                return _completed(command, 1, stderr="error during connect: daemon unavailable")
            state.remove(CID)
            return _completed(command, 0, CID + "\n")
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)
    cleaned, record = esc._remove_container(
        CID,
        solver_task_id=TASK_ID,
        expected_owner="claude-code-external",
        cidfile=None,
    )

    assert cleaned is True
    assert record["status"] == "removed"
    assert remove_calls == 2


def test_network_cleanup_retries_transient_inspect_failure(monkeypatch) -> None:
    inspect_calls = 0

    def docker(command: list[str], **_kwargs: object):
        nonlocal inspect_calls
        if command[:3] == ["docker", "network", "inspect"]:
            inspect_calls += 1
            if inspect_calls == 1:
                return _completed(command, 1, stderr="Cannot connect to the Docker daemon")
            if inspect_calls == 2:
                return _completed(
                    command,
                    0,
                    json.dumps(
                        {
                            "opencollab.owner": "claude-code-network",
                            "opencollab.solver_task_id": TASK_ID,
                        }
                    ),
                )
            return _completed(command, 1, stderr="Error: No such network")
        if command[:3] == ["docker", "network", "rm"]:
            return _completed(command, 0, NETWORK + "\n")
        raise AssertionError(command)

    monkeypatch.setattr(esc.subprocess, "run", docker)
    result = esc._cleanup_network(NETWORK, TASK_ID)

    assert result["proven"] is True
    assert result["status"] == "removed"
    assert inspect_calls == 3


def test_docker_retries_share_one_wall_clock_budget(monkeypatch) -> None:
    clock = 100.0
    timeouts: list[float] = []

    def monotonic() -> float:
        return clock

    def docker(_command: list[str], *, timeout: float, **_kwargs: object):
        nonlocal clock
        timeouts.append(timeout)
        # Simulate each transient attempt consuming one second of the budget.
        clock += 1.0
        return _completed([], 1, stderr="Cannot connect to the Docker daemon")

    monkeypatch.setattr(esc.time, "monotonic", monotonic)
    monkeypatch.setattr(esc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(esc.subprocess, "run", docker)

    result = esc._docker(["docker", "inspect"], 5)

    assert result is not None
    assert len(timeouts) == 3
    assert timeouts[0] == 5.0
    assert timeouts == sorted(timeouts, reverse=True)
    assert max(timeouts) <= 5.0
    # The final attempt receives only the remaining budget, rather than a
    # fresh five-second timeout that would permit a 3x wall-clock overrun.
    assert timeouts[-1] == 3.0
