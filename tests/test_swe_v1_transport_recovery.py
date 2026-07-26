from __future__ import annotations

from types import SimpleNamespace

import opencollab_eval.commands.swe_v1_prolite_runner as runner


def _eval_only_args() -> SimpleNamespace:
    return SimpleNamespace(
        ssh_command="ssh",
        eval_only=True,
        no_sync_runtime=True,
        host="example",
        remote_proxy_base_url="http://remote",
        remote_runtime_repo="/remote/repo",
        remote_root="/remote",
        base_run_dir="/remote/run",
        workflow="team-pro",
        model_name="model",
        session_prefix="session",
        image_repository="registry.example/swebench",
        start_index=1,
        limit=1,
        budget=1000,
        max_steps=3,
        swe_timeout=10,
        task_wall_timeout=10,
        eval_timeout=10,
        llm_timeout=10,
        checkpoint_interval=0,
        max_task_starts=1,
        dry_run=False,
        total_timeout=30,
    )


def test_wait_for_terminal_summary_survives_a_transport_outage(monkeypatch):
    observations = iter(
        [
            None,
            {"runner_state": "alive", "summary": {"status": "running"}},
            {"runner_state": "dead", "summary": {"status": "done"}},
        ]
    )
    clock = [0.0]
    monkeypatch.setattr(
        runner, "probe_remote_execution_state", lambda **kwargs: next(observations)
    )
    monkeypatch.setattr(runner, "remote_summary_matches_payload", lambda *args: True)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runner.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    summary = runner.wait_for_terminal_remote_summary(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
        remote_runtime_repo="/remote/runtime",
        owner_nonce="a" * 32,
        payload={},
        deadline=300,
    )

    assert summary == {"status": "done"}


def test_run_remote_recovers_after_primary_ssh_transport_loss(monkeypatch):
    class ExitedProcess:
        pid = 4321
        returncode = 255

    cleanup_calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda *args, **kwargs: ("", "ssh: connect to host example: Connection refused"),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pid: False)
    monkeypatch.setattr(
        runner,
        "wait_for_terminal_remote_summary",
        lambda **kwargs: {"status": "done", "counts": {"technical_failed": 0}},
    )
    monkeypatch.setattr(runner, "terminate_local_process_group", lambda proc: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: (cleanup_calls.append(kwargs) or {"ok": True}, None),
    )

    summary = runner.run_remote(_eval_only_args())

    assert cleanup_calls == []
    assert summary["status"] == "done"
    assert summary["remote_transport"]["reason"] == "primary_transport_lost"
