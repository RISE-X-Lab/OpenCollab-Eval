from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import opencollab_eval.commands.swe_v1_prolite_runner as runner
import opencollab_eval.commands.swe_v1_transport_recovery as recovery
from opencollab_eval.engine.swe_v1_runner_claim import runner_claim_sha256


def _eval_only_args() -> SimpleNamespace:
    return SimpleNamespace(
        ssh_command="ssh",
        eval_only=True,
        no_sync_runtime=True,
        expected_runtime_tree_sha256="a" * 64,
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


@pytest.fixture(autouse=True)
def _verified_runtime(monkeypatch):
    monkeypatch.setattr(
        runner,
        "verify_remote_runtime",
        lambda **kwargs: {"sha256": "a" * 64},
    )
    monkeypatch.setattr(
        runner._controller,
        "recover_existing_remote_summary",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        runner._controller,
        "probe_preexisting_remote_execution",
        lambda **kwargs: None,
    )


def test_wait_for_terminal_summary_survives_a_transport_outage(monkeypatch):
    payload = {"owner_nonce": "a" * 32, "invocation_id": "c" * 32}
    owner = {
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "a" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
    }
    observations = iter(
        [
            None,
            None,
            {
                "runner_state": "dead",
                "runner_owner": owner,
                "summary": {"status": "done"},
            },
        ]
    )
    clock = [0.0]
    monkeypatch.setattr(
        runner, "probe_remote_execution_state", lambda **kwargs: next(observations)
    )
    monkeypatch.setattr(
        runner,
        "remote_summary_matches_payload",
        lambda *args, **kwargs: True,
    )
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
        payload=payload,
        deadline=300,
    )

    assert summary == {"status": "done"}


def test_wait_for_terminal_summary_keeps_ownership_through_repeated_probe_failures(
    monkeypatch,
):
    payload = {"owner_nonce": "a" * 32, "invocation_id": "c" * 32}
    owner = {
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "a" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
    }
    observations = iter(
        [
            None,
            None,
            None,
            None,
            {"runner_state": "alive", "runner_owner": owner, "summary": None},
            {
                "runner_state": "dead",
                "runner_owner": owner,
                "summary": {"status": "done"},
            },
        ]
    )
    clock = [0.0]
    monkeypatch.setattr(
        runner, "probe_remote_execution_state", lambda **kwargs: next(observations)
    )
    monkeypatch.setattr(
        runner,
        "remote_summary_matches_payload",
        lambda *args, **kwargs: True,
    )
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
        payload=payload,
        deadline=1_000,
    )

    assert summary == {"status": "done"}
    assert clock[0] == runner.REMOTE_COMPLETION_POLL_SECONDS * 5


def test_wait_for_terminal_summary_stops_only_at_the_task_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        runner, "probe_remote_execution_state", lambda **kwargs: None
    )
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

    assert summary is None
    assert clock[0] == 300


def test_prelaunch_probe_keeps_the_worker_slot_until_ownership_is_known(monkeypatch):
    observations = iter([None, None, {"runner_state": "missing", "summary": None}])
    clock = [0.0]
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: next(observations),
    )
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        recovery.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    observed = recovery.wait_for_remote_ownership_fact(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
        remote_runtime_repo="/remote/runtime",
        remote_python="python3",
        deadline=300,
    )

    assert observed == {"runner_state": "missing", "summary": None}
    assert clock[0] == 240


def test_prelaunch_probe_handles_a_missing_remote_runtime_on_first_run(tmp_path):
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "os.execv('/bin/sh', ['sh', '-c', sys.argv[2]])\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    observed = recovery.probe_remote_execution_state(
        ssh_command=[str(fake_ssh)],
        host="unused-host",
        base_run_dir=str(tmp_path / "new-run"),
        remote_runtime_repo=str(tmp_path / "runtime-does-not-exist"),
        remote_python=sys.executable,
    )

    assert observed == {
        "runner_state": "missing",
        "runner_owner": None,
        "summary": None,
    }


def test_probe_remote_execution_state_honors_caller_timeout(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"runner_state": "missing", "runner_owner": None, "summary": None}
            ),
            stderr="",
        )

    monkeypatch.setattr(recovery.subprocess, "run", fake_run)

    observed = recovery.probe_remote_execution_state(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
        timeout=0.25,
    )

    assert observed is not None
    assert calls[0]["timeout"] == pytest.approx(0.25)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), 0.0])
def test_probe_remote_execution_state_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        recovery.probe_remote_execution_state(
            ssh_command=["ssh"],
            host="example",
            base_run_dir="/remote/run",
            timeout=timeout,
        )


def test_prelaunch_probe_clamps_subprocess_timeout_to_remaining_deadline(monkeypatch):
    calls = []
    clock = [10.0]

    def probe(**kwargs):
        calls.append(kwargs["timeout"])
        return None

    monkeypatch.setattr(recovery, "probe_remote_execution_state", probe)
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        recovery.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(TimeoutError):
        recovery.wait_for_remote_ownership_fact(
            ssh_command=["ssh"],
            host="example",
            base_run_dir="/remote/run",
            remote_runtime_repo="/remote/runtime",
            remote_python="python3",
            deadline=12.0,
        )

    assert calls == [pytest.approx(2.0)]


def test_prelaunch_probe_caps_timeout_for_large_remaining_budget(monkeypatch):
    calls = []

    def probe(**kwargs):
        calls.append(kwargs["timeout"])
        return {"runner_state": "missing", "summary": None}

    monkeypatch.setattr(recovery, "probe_remote_execution_state", probe)

    observed = recovery.wait_for_remote_ownership_fact(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
        remote_runtime_repo="/remote/runtime",
        remote_python="python3",
        deadline=10**9,
    )

    assert observed == {"runner_state": "missing", "summary": None}
    assert calls == [pytest.approx(recovery.REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS)]


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


def test_run_remote_keeps_primary_transport_during_side_probe_outage(monkeypatch):
    class ExitedProcess:
        pid = 4321
        returncode = 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    probe_timeouts = []

    def probe(**kwargs):
        probe_timeouts.append(kwargs["timeout"])
        return None

    monkeypatch.setattr(runner, "probe_remote_execution_state", probe)

    def unavailable_side_probes(*args, poll_callback, **kwargs):
        for _ in range(4):
            poll_callback()
        return '{"status":"done"}', ""

    monkeypatch.setattr(runner, "_bounded_remote_communicate", unavailable_side_probes)
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pid: False)

    summary = runner.run_remote(_eval_only_args())

    assert summary["status"] == "done"
    assert probe_timeouts
    assert all(0 < timeout <= 30 for timeout in probe_timeouts)


def test_existing_live_runner_is_adopted_by_its_full_owner_identity(monkeypatch):
    payload = {"invocation_id": "c" * 32}
    owner = {
        "schema": "opencollab.prolite_runner_owner.v1",
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
    }
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "alive",
            "runner_owner": owner,
            "summary": None,
        },
    )
    waited = []

    def wait_for_owner(**kwargs):
        waited.append(kwargs)
        return {"status": "done"}

    monkeypatch.setattr(recovery, "wait_for_terminal_remote_summary", wait_for_owner)

    summary = recovery.recover_existing_remote_summary(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
        remote_runtime_repo="/remote/runtime",
        remote_python="python3",
        payload=payload,
        deadline=10**9,
    )

    assert summary == {"status": "done"}
    assert waited[0]["owner_nonce"] == "b" * 32
    assert waited[0]["expected_owner"] == (
        4321,
        "proc:12345",
        "b" * 32,
        runner_claim_sha256(payload),
        "c" * 32,
    )


def test_existing_live_runner_must_match_the_task_claim(monkeypatch):
    owner_payload = {"start_index": 52, "limit": 1, "invocation_id": "c" * 32}
    owner = {
        "schema": "opencollab.prolite_runner_owner.v1",
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(owner_payload),
        "invocation_id": "c" * 32,
    }
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "alive",
            "runner_owner": owner,
            "summary": None,
        },
    )

    with pytest.raises(recovery.RemoteRunnerUnavailable):
        recovery.recover_existing_remote_summary(
            ssh_command=["ssh"],
            host="example",
            base_run_dir="/remote/run",
            remote_runtime_repo="/remote/runtime",
            remote_python="python3",
            payload={"start_index": 51, "limit": 1, "invocation_id": "c" * 32},
            deadline=10**9,
        )


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("dry_run", True),
        ("remote_root", "/another/dataset-root"),
        ("image_repository", "registry.example/another-suite"),
    ],
)
def test_existing_live_runner_rejects_execution_semantic_claim_mismatch(
    monkeypatch,
    field,
    other,
):
    payload = {
        "invocation_id": "c" * 32,
        "dry_run": False,
        "remote_root": "/remote",
        "image_repository": "registry.example/swebench",
    }
    owner = {
        "schema": "opencollab.prolite_runner_owner.v1",
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
    }
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "alive",
            "runner_owner": owner,
            "summary": None,
        },
    )
    changed = dict(payload)
    changed[field] = other

    with pytest.raises(recovery.RemoteRunnerUnavailable):
        recovery.recover_existing_remote_summary(
            ssh_command=["ssh"],
            host="example",
            base_run_dir="/remote/run",
            remote_runtime_repo="/remote/runtime",
            remote_python="python3",
            payload=changed,
            deadline=10**9,
        )


def test_existing_runner_rejects_a_terminal_summary_from_another_invocation(
    monkeypatch,
):
    payload = {
        "start_index": 51,
        "limit": 1,
        "base_run_dir": "/remote/run",
        "remote_repo": "/remote/runtime",
        "remote_python": "python3",
        "invocation_id": "c" * 32,
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "model",
        "llm_model": "glm-5.2",
        "llm_provider": "anthropic",
        "context_window": 400000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32768,
        "budget": 4000000,
        "max_steps": 60,
        "max_task_starts": 1,
        "max_empty_patch_retries": 1,
        "max_eval_attempts": 2,
        "eval_only": False,
        "eval_dir_name": "official_eval",
    }
    owner = {
        "schema": "opencollab.prolite_runner_owner.v1",
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
    }
    summary = dict(recovery._remote_summary_expectation(payload))
    summary["status"] = "done"
    summary["invocation_id"] = "d" * 32
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "dead",
            "runner_owner": owner,
            "summary": summary,
        },
    )

    with pytest.raises(recovery.RemoteRunnerUnavailable):
        recovery.recover_existing_remote_summary(
            ssh_command=["ssh"],
            host="example",
            base_run_dir="/remote/run",
            remote_runtime_repo="/remote/runtime",
            remote_python="python3",
            payload=payload,
            deadline=10**9,
        )


def test_run_remote_rebuilds_report_from_an_existing_runner_without_launch(
    monkeypatch,
):
    payload = {
        "base_run_dir": "/remote/run",
        "remote_repo": "/remote/repo",
        "remote_python": "python3",
        "start_index": 1,
        "limit": 1,
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "model",
        "llm_model": "",
        "llm_provider": "anthropic",
        "llm_transport": "reverse_proxy",
        "budget": 1000,
        "max_steps": 3,
        "max_task_starts": 1,
        "max_empty_patch_retries": 1,
        "max_eval_attempts": 2,
        "eval_only": True,
        "eval_dir_name": "official_eval",
        "runtime_tree_sha256": "a" * 64,
        "remote_root": "/remote",
        "session_prefix": "session",
        "image_repository": "registry.example/swebench",
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "llm_timeout": 10,
        "dry_run": False,
        "invocation_id": "c" * 32,
    }
    owner = {
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(payload),
        "invocation_id": "c" * 32,
        "runtime_tree_sha256": "a" * 64,
    }
    recovered = {
        "status": "done_with_technical_failures",
        "counts": {"technical_failed": 1, "generation_done": 0},
        "rows": [
            {
                "generation": {
                    "status": "generation_failed",
                    "observed_patch_chars": 6906,
                }
            }
        ],
    }
    recovery_calls = []

    def recover_owner(**kwargs):
        recovery_calls.append(kwargs)
        return recovered

    monkeypatch.setattr(
        runner,
        "recover_existing_remote_summary",
        recover_owner,
    )
    monkeypatch.setattr(
        runner,
        "probe_preexisting_remote_execution",
        lambda **kwargs: {
            "runner_state": "alive",
            "runner_owner": owner,
            "summary": None,
        },
    )
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("matching remote owner must be recovered before launch")
        ),
    )

    summary = runner.run_remote(_eval_only_args())

    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["technical_failed"] == 1
    assert summary["rows"][0]["generation"]["status"] == "generation_failed"
    assert "submission_integrity" not in summary["rows"][0]["generation"]
    assert "source_patch_sha256" not in summary["rows"][0]["generation"]
    assert summary["remote_transport"]["reason"] == "existing_remote_owner"
    assert recovery_calls[0]["payload"]["invocation_id"] == "c" * 32
    assert runner_claim_sha256(recovery_calls[0]["payload"]) == owner["claim_sha256"]


def test_run_remote_adopts_preexisting_owner_through_real_terminal_validation(
    monkeypatch,
):
    args = _eval_only_args()
    owner_payload = {
        "token": "",
        "remote_api_env_file": "",
        "llm_transport": "reverse_proxy",
        "owner_nonce": "b" * 32,
        "remote_root": "/remote",
        "remote_repo": "/remote/repo",
        "remote_python": "python3",
        "base_run_dir": "/remote/run",
        "workflow": "team-pro",
        "workflow_env": {},
        "openhands_command": "",
        "openhands_empty_patch_rejections": 2,
        "max_empty_patch_retries": 1,
        "model_name": "model",
        "llm_model": "",
        "llm_provider": "anthropic",
        "context_window": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "invocation_id": "c" * 32,
        "run_id": "",
        "runtime_tree_sha256": "a" * 64,
        "session_prefix": "session",
        "image_repository": "registry.example/swebench",
        "remote_proxy_base_url": "http://remote",
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
        "max_eval_attempts": 2,
        "eval_only": True,
        "eval_dir_name": "official_eval",
        "expected_task": "",
        "expected_record_id": "",
        "expected_source_patch_sha256": "",
        "expected_eval_patch_sha256": "",
        "dry_run": False,
    }
    owner = {
        "pid": 4321,
        "start_identity": "proc:12345",
        "owner_nonce": "b" * 32,
        "claim_sha256": runner_claim_sha256(owner_payload),
        "invocation_id": "c" * 32,
        "runtime_tree_sha256": "a" * 64,
    }
    terminal = recovery._remote_summary_expectation(owner_payload)
    terminal["status"] = "done"
    observations = iter(
        [
            {"runner_state": "alive", "runner_owner": owner, "summary": None},
            {"runner_state": "alive", "runner_owner": owner, "summary": None},
            {"runner_state": "dead", "runner_owner": owner, "summary": terminal},
        ]
    )
    monkeypatch.setattr(
        runner,
        "probe_preexisting_remote_execution",
        lambda **kwargs: next(observations),
    )
    monkeypatch.setattr(
        recovery,
        "probe_remote_execution_state",
        lambda **kwargs: next(observations),
    )
    monkeypatch.setattr(
        runner._controller,
        "recover_existing_remote_summary",
        recovery.recover_existing_remote_summary,
    )
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a matching owner must not start another runner")
        ),
    )

    summary = runner.run_remote(args)

    assert summary["status"] == "done"
    assert summary["invocation_id"] == "c" * 32
    assert summary["remote_transport"]["reason"] == "existing_remote_owner"
