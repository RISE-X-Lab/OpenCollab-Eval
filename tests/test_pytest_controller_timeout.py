from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from swe_v1_prolite_runner_test_support import _remote_namespace

from opencollab_eval.engine.swe_v1_remote_pytest_controller import (
    prolite_pytest_controller_source,
)


def _controller() -> dict[str, object]:
    namespace: dict[str, object] = {"__name__": "controller_test"}
    exec(prolite_pytest_controller_source(), namespace)
    return namespace


def _valid_argv(proof: Path, timeout: str) -> list[str]:
    command = ["pytest", "-q"]
    digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
    return [
        "controller.py",
        "--proof-output",
        str(proof),
        "--command-sha256",
        digest,
        f"--event-timeout-seconds={timeout}",
        "--",
        *command,
    ]


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf", "86401"])
def test_controller_rejects_invalid_event_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeout: str
) -> None:
    namespace = _controller()
    monkeypatch.setattr(sys, "argv", _valid_argv(tmp_path / "proof.jsonl", timeout))

    with pytest.raises(ValueError, match="event timeout"):
        namespace["_arguments"]()  # type: ignore[operator]


def test_controller_event_collection_times_out_for_a_silent_worker() -> None:
    namespace = _controller()
    read_fd, write_fd = os.pipe()

    try:
        with pytest.raises(ValueError, match="event stream timed out"):
            namespace["_collect_events"](  # type: ignore[operator]
                SimpleNamespace(poll=lambda: None), read_fd, 0.01
            )
    finally:
        os.close(write_fd)


def test_controller_does_not_wait_forever_after_worker_closes_event_pipe() -> None:
    namespace = _controller()
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    class SilentAfterEof:
        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise namespace["subprocess"].TimeoutExpired("worker", timeout)  # type: ignore[index]

    try:
        with pytest.raises(ValueError, match="did not exit before event timeout"):
            namespace["_collect_events"](  # type: ignore[operator]
                SilentAfterEof(), read_fd, 0.01
            )
    finally:
        # _collect_events owns and closes read_fd on both success and failure.
        pass


def test_controller_main_removes_reserved_proof_after_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _controller()
    proof = tmp_path / "proof.jsonl"
    proof.write_text("reserved\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", _valid_argv(proof, "1"))
    home = tmp_path / "home"
    home.mkdir()
    killed: list[int] = []

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.wait_calls: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            return 86

    process = FakeProcess()
    monkeypatch.setitem(namespace, "_prepare_output", lambda path, root: (1, 2, 3))
    monkeypatch.setitem(namespace, "_prepare_worker", lambda repo: home)
    monkeypatch.setitem(namespace, "_trusted_worker_command", lambda *args: ["worker"])
    monkeypatch.setitem(
        namespace,
        "_collect_events",
        lambda *args: (_ for _ in ()).throw(ValueError("event stream timed out")),
    )
    monkeypatch.setitem(namespace, "_kill_surviving_group", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: process)  # type: ignore[index]

    assert namespace["main"]() == 86  # type: ignore[operator]
    assert not proof.exists()
    assert killed == [4242]
    assert process.wait_calls == [1]


def test_pytest_plan_passes_the_eval_timeout_to_the_controller(tmp_path: Path) -> None:
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](  # type: ignore[operator]
        {"repo_language": "python"}, ["tests/test_target.py::test_target"]
    )

    script = namespace["prolite_test_plan_script"](  # type: ignore[operator]
        plan, "f2p", "nonce", controller_timeout=7
    )

    # The controller is wrapped in the portable process-group watchdog.  The
    # inner command is hex-encoded to preserve its shell quoting, so inspect
    # the encoded payload rather than coupling this test to an unescaped
    # implementation detail.
    assert b"--event-timeout-seconds 7".hex() in script
