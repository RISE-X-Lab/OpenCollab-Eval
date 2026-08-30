"""Regression tests for bounded SWE runner transport and launcher behavior."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from package_test_support import resource_path
from swe_v1_prolite_runner_test_support import _remote_namespace

import opencollab_eval.commands.swe_v1_prolite_runner as runner
from opencollab_eval.engine import swe_v1_remote_commands as remote_commands


def _eval_only_args(**overrides):
    values = {
        "ssh_command": "ssh",
        "eval_only": True,
        "no_sync_runtime": True,
        "expected_runtime_tree_sha256": "a" * 64,
        "host": "example",
        "remote_proxy_base_url": "http://remote",
        "remote_runtime_repo": "/remote/repo",
        "remote_root": "/remote",
        "base_run_dir": "/remote/run",
        "workflow": "team-pro",
        "model_name": "model",
        "session_prefix": "session",
        "image_repository": "registry.example/swebench",
        "start_index": 1,
        "limit": 1,
        "budget": 1000,
        "max_steps": 3,
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "llm_timeout": 10,
        "checkpoint_interval": 0,
        "max_task_starts": 1,
        "dry_run": False,
        "total_timeout": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _verified_runtime(monkeypatch):
    monkeypatch.setattr(runner, "verify_remote_runtime", lambda **kwargs: {"sha256": "a" * 64})
    monkeypatch.setattr(runner._controller, "recover_existing_remote_summary", lambda **kwargs: None)
    monkeypatch.setattr(runner._controller, "probe_preexisting_remote_execution", lambda **kwargs: None)


def test_run_remote_uses_remaining_end_to_end_timeout_after_preflight(monkeypatch):
    """Primary SSH must inherit the time left after runtime verification."""
    clock = [100.0]
    captured: dict[str, float] = {}
    monkeypatch.setattr(runner._controller.time, "monotonic", lambda: clock[0])

    def slow_verify(**kwargs):
        clock[0] += 12.0
        return {"sha256": "a" * 64}

    monkeypatch.setattr(runner._controller, "verify_remote_runtime", slow_verify)

    class CompletedProcess:
        pid = 4321
        returncode = 0

    monkeypatch.setattr(runner._controller.subprocess, "Popen", lambda *args, **kwargs: CompletedProcess())
    monkeypatch.setattr(runner._controller, "_block_local_spawn_signals", lambda: {})
    monkeypatch.setattr(runner._controller, "_restore_local_spawn_signals", lambda _state: None)
    monkeypatch.setattr(runner._controller, "_local_process_group_exists", lambda _pid: False)

    def communicate(proc, payload, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return '{"status":"done"}', ""

    monkeypatch.setattr(runner._controller, "_bounded_remote_communicate", communicate)
    summary = runner.run_remote(_eval_only_args())

    assert summary["status"] == "done"
    assert captured["timeout"] == pytest.approx(6.0)


def test_remaining_timeout_rejects_expired_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(runner._controller.time, "monotonic", lambda: clock[0])

    assert runner._controller._remaining_timeout(130.0) == pytest.approx(30.0)
    clock[0] = 130.0
    with pytest.raises(subprocess.TimeoutExpired):
        runner._controller._remaining_timeout(130.0)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "bad"])
def test_fifo_writer_rejects_invalid_timeout_without_opening(monkeypatch, tmp_path, timeout):
    opened = False

    def fail_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("invalid timeout must fail before opening the FIFO")

    monkeypatch.setattr(remote_commands.os, "open", fail_open)
    result = remote_commands.write_fifo_with_timeout(tmp_path / "token.fifo", "token", timeout)

    assert result == {"ok": False, "error": "fifo timeout must be finite and positive"}
    assert not opened


def test_fifo_writer_uses_monotonic_deadline(monkeypatch, tmp_path):
    clock = [100.0]
    sleeps = []

    monkeypatch.setattr(remote_commands.time, "monotonic", lambda: clock[0])
    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(remote_commands.time, "sleep", fake_sleep)

    def no_reader(*_args, **_kwargs):
        raise OSError("no reader")

    monkeypatch.setattr(remote_commands.os, "open", no_reader)
    result = remote_commands.write_fifo_with_timeout(tmp_path / "token.fifo", "token", 0.3)

    assert result["ok"] is False
    assert result["error"] == "no reader"
    assert clock[0] >= 100.3
    assert sleeps


def test_fifo_writer_default_matches_shell_reader_budget():
    assert remote_commands.write_fifo_with_timeout.__defaults__ == (120,)
    shell = resource_path("run_swe_v2_one_from_fifo.sh").read_text(encoding="utf-8")
    assert 'OPENCOLLAB_PROXY_TOKEN_TIMEOUT_SECONDS:-120' in shell


def test_public_preparation_timeout_is_forwarded_only_to_eval_container(tmp_path):
    # The controller's remote namespace is installed separately in the
    # evaluation worker; this assertion exercises the same allowlisted value
    # forwarding without starting Docker.
    remote_namespace = _remote_namespace(
        tmp_path,
        workflow_env={"OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS": "17"},
    )

    assert remote_namespace["_public_preparation_docker_env"]() == [
        "--env",
        "OPENCOLLAB_PUBLIC_PREPARATION_TIMEOUT_SECONDS=17",
    ]


@pytest.mark.parametrize(
    "total_timeout",
    [0, -1, float("nan"), float("inf"), float("-inf"), True, "invalid"],
)
def test_run_remote_rejects_invalid_programmatic_total_timeout(total_timeout):
    with pytest.raises(ValueError, match="total_timeout must be finite and positive"):
        runner.run_remote(_eval_only_args(total_timeout=total_timeout))


def test_run_remote_records_preflight_timeout_instead_of_raising(monkeypatch):
    monkeypatch.setattr(
        runner._controller,
        "probe_preexisting_remote_execution",
        lambda **kwargs: (_ for _ in ()).throw(
            TimeoutError("ownership probe deadline exhausted")
        ),
    )

    summary = runner.run_remote(_eval_only_args(total_timeout=1))

    assert summary["status"] == "preflight_failed"
    assert summary["technical_reasons"] == ["remote_ownership_timeout"]
    assert summary["remote_transport"]["phase"] == "preexisting_owner_probe"


def test_run_remote_records_existing_owner_recovery_timeout(monkeypatch):
    monkeypatch.setattr(
        runner._controller,
        "recover_existing_remote_summary",
        lambda **kwargs: (_ for _ in ()).throw(
            TimeoutError("recovery probe deadline exhausted")
        ),
    )
    monkeypatch.setattr(
        runner._controller,
        "probe_preexisting_remote_execution",
        lambda **kwargs: {
            "runner_state": "dead",
            "runner_owner": {
                "pid": 4321,
                "start_identity": "proc:1",
                "owner_nonce": "b" * 32,
                "claim_sha256": "c" * 64,
                "invocation_id": "d" * 32,
            },
            "summary": None,
        },
    )
    monkeypatch.setattr(
        runner._controller,
        "runner_owner_identity",
        lambda observed: (4321, "proc:1", "b" * 32, "c" * 64, "d" * 32),
    )
    monkeypatch.setattr(
        runner._controller, "_recovery_runtime_tree", lambda observed: "e" * 64
    )
    monkeypatch.setattr(
        runner._controller, "_recovery_invocation_id", lambda observed: "d" * 32
    )

    summary = runner.run_remote(
        _eval_only_args(total_timeout=1, expected_runtime_tree_sha256="e" * 64)
    )

    assert summary["status"] == "preflight_failed"
    assert summary["remote_transport"]["phase"] == "existing_owner_recovery_probe"


def test_run_remote_clamps_runtime_proxy_preflight_to_shared_deadline(monkeypatch):
    captured = {}

    def timeout_proxy(**kwargs):
        captured.update(kwargs)
        raise TimeoutError("proxy setup deadline exhausted")

    monkeypatch.setattr(runner._controller, "ensure_remote_proxy", timeout_proxy)
    summary = runner.run_remote(
        _eval_only_args(
            eval_only=False,
            no_sync_runtime=True,
            no_ensure_remote_proxy=False,
            remote_api_env_file="",
            local_proxy_base_url="http://127.0.0.1:8080",
            proxy_env_file="/unused/token.env",
        )
    )

    assert summary["status"] == "preflight_failed"
    assert summary["remote_transport"]["phase"] == "remote_proxy_setup"
    assert captured["deadline"] > time.monotonic()


def _shell_env(root: Path, *, timeout: str = "120") -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
            "OPENCOLLAB_REMOTE_ROOT": str(root),
            "OPENCOLLAB_REMOTE_REPO": str(root),
            "OPENCOLLAB_PROXY_TOKEN_TIMEOUT_SECONDS": timeout,
        }
    )
    return env


def _run_fifo(fifo: Path, run_dir: Path, env: dict[str, str], *, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(resource_path("run_swe_v2_one_from_fifo.sh")), "instance", "image", str(fifo), str(run_dir)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_generation_shell_forwards_safe_empty_arrays_and_fifo_timeout(tmp_path):
    shell = resource_path("run_swe_v2_one_from_fifo.sh").read_text(encoding="utf-8")
    assert '${llm_args[@]+"${llm_args[@]}"}' in shell
    assert '${openhands_args[@]+"${openhands_args[@]}"}' in shell
    fifo = tmp_path / "proxy-token.fifo"
    os.mkfifo(fifo, 0o600)
    started = time.monotonic()
    completed = _run_fifo(fifo, tmp_path / "run", _shell_env(tmp_path, timeout="0.2"))
    assert completed.returncode == 124
    assert "timed out waiting for proxy token" in completed.stderr
    assert time.monotonic() - started < 3
    assert not fifo.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_generation_shell_token_fifo_times_out_for_a_silent_writer(tmp_path):
    fifo = tmp_path / "proxy-token.fifo"
    os.mkfifo(fifo, 0o600)
    writer = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; handle = open(sys.argv[1], 'w'); time.sleep(2)", str(fifo)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        completed = _run_fifo(fifo, tmp_path / "run", _shell_env(tmp_path, timeout="0.2"))
    finally:
        writer.terminate()
        writer.wait(timeout=5)
    assert completed.returncode == 124
    assert "timed out waiting for proxy token" in completed.stderr
    assert not fifo.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_generation_shell_runs_with_empty_optional_argument_arrays(tmp_path):
    base = tmp_path / "base"
    repo = tmp_path / "repo"
    instance = base / "datasets" / "swe-batch-pro-lite" / "instances" / "instance.json"
    instance.parent.mkdir(parents=True)
    instance.write_text("{}\n", encoding="utf-8")
    package = repo / "src" / "opencollab_eval" / "generation"
    package.mkdir(parents=True)
    (repo / "src" / "opencollab_eval" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "gen_prediction_workflow.py").write_text(
        "import os\nprint('token=' + os.environ['OPENCOLLAB_API_KEY'])\n", encoding="utf-8"
    )
    fifo = tmp_path / "proxy-token.fifo"
    os.mkfifo(fifo, 0o600)
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; handle = open(sys.argv[1], 'w'); handle.write('token-ok\\n'); handle.flush(); handle.close()",
            str(fifo),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = _shell_env(base, timeout="2")
    env.update(
        {
            "OPENCOLLAB_REMOTE_REPO": str(repo),
            "OPENCOLLAB_REMOTE_PROXY_BASE_URL": "http://127.0.0.1:1",
            "OPENCOLLAB_MODEL": "test-model",
        }
    )
    try:
        completed = _run_fifo(fifo, tmp_path / "run", env)
    finally:
        writer.wait(timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert "token=token-ok" in completed.stdout
    assert not fifo.exists()


def test_generation_shell_extracts_instances_streaming_and_atomically():
    shell = resource_path("run_swe_v2_one_from_fifo.sh").read_text(encoding="utf-8")
    assert 'with source.open("r", encoding="utf-8", errors="replace")' in shell
    assert "tempfile.mkstemp(" in shell
    assert "os.replace(temporary, target)" in shell


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_generation_shell_fallback_publishes_complete_instance_file(tmp_path):
    base = tmp_path / "base"
    repo = tmp_path / "repo"
    dataset = base / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        '{"instance_id":"other","payload":"ignored"}\n'
        '{"instance_id":"instance","payload":"selected"}\n',
        encoding="utf-8",
    )
    package = repo / "src" / "opencollab_eval" / "generation"
    package.mkdir(parents=True)
    (repo / "src" / "opencollab_eval" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "gen_prediction_workflow.py").write_text(
        "import pathlib, sys\n"
        "assert pathlib.Path(sys.argv[sys.argv.index('--instance-file') + 1]).read_text() == "
        "'{\"instance_id\": \"instance\", \"payload\": \"selected\"}\\n'\n",
        encoding="utf-8",
    )
    fifo = tmp_path / "proxy-token.fifo"
    os.mkfifo(fifo, 0o600)
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; handle = open(sys.argv[1], 'w'); handle.write('token-ok\\n'); handle.close()",
            str(fifo),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = _shell_env(base, timeout="2")
    env.update(
        {
            "OPENCOLLAB_REMOTE_REPO": str(repo),
            "OPENCOLLAB_REMOTE_PROXY_BASE_URL": "http://127.0.0.1:1",
            "OPENCOLLAB_MODEL": "test-model",
        }
    )
    try:
        completed = _run_fifo(fifo, tmp_path / "run", env)
    finally:
        writer.wait(timeout=5)
    assert completed.returncode == 0, completed.stderr
    instance_file = tmp_path / "run" / "instance_files" / "instance.json"
    assert instance_file.read_text(encoding="utf-8") == (
        '{"instance_id": "instance", "payload": "selected"}\n'
    )
    assert not list(instance_file.parent.glob(".*"))


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_generation_shell_rejects_nonpositive_or_nonfinite_token_timeout(tmp_path, timeout):
    fifo = tmp_path / "proxy-token.fifo"
    os.mkfifo(fifo, 0o600)
    completed = _run_fifo(fifo, tmp_path / "run", _shell_env(tmp_path, timeout=timeout))
    assert completed.returncode == 2
    assert "finite and positive" in completed.stderr
    assert not fifo.exists()
