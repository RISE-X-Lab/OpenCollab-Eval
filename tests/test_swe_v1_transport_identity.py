from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from test_swe_v1_transport_recovery import _eval_only_args

import opencollab_eval.commands.swe_v1_prolite_runner as runner


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


def _summary_pair(*, task_starts: int, eval_only: bool, eval_dir: str):
    payload = {
        "start_index": 31,
        "limit": 1,
        "base_run_dir": "/remote/run/task_31",
        "remote_repo": "/remote/runtime",
        "remote_python": "/remote/venv/bin/python",
        "invocation_id": "a" * 32,
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "teampro-model",
        "llm_model": "glm-5.2",
        "llm_provider": "anthropic",
        "context_window": 400000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32768,
        "budget": 4000000,
        "max_steps": 60,
        "max_task_starts": task_starts,
        "max_empty_patch_retries": 0 if eval_only else 1,
        "max_eval_attempts": 2,
        "eval_container_bind_timeout": 30,
        "eval_only": eval_only,
        "eval_dir_name": eval_dir,
    }
    summary = {
        "slice": "31",
        "base_run_dir": "/remote/run/task_31",
        "remote_runtime_repo": "/remote/runtime",
        "remote_python": "/remote/venv/bin/python",
        "solver_attribution": (
            "historical_artifact" if eval_only else "current_run"
        ),
        **{
            key: payload[key]
            for key in (
                "invocation_id",
                "workflow",
                "workflow_env",
                "model_name",
                "llm_model",
                "llm_provider",
                "context_window",
                "temperature",
                "top_p",
                "max_output_tokens",
                "budget",
                "max_steps",
                "max_task_starts",
                "max_empty_patch_retries",
                "max_eval_attempts",
                "eval_container_bind_timeout",
                "eval_only",
                "eval_dir_name",
            )
        },
    }
    return payload, summary


def test_remote_summary_matches_payload_rejects_stale_runtime_identity():
    payload, summary = _summary_pair(
        task_starts=3,
        eval_only=False,
        eval_dir="official_eval",
    )

    assert runner.remote_summary_matches_payload(summary, payload) is True
    summary["invocation_id"] = "b" * 32
    assert runner.remote_summary_matches_payload(summary, payload) is False
    summary["invocation_id"] = "a" * 32
    summary["budget"] = 16000000
    assert runner.remote_summary_matches_payload(summary, payload) is False
    summary["budget"] = 4000000
    summary["remote_python"] = "/another/runtime/bin/python"
    assert runner.remote_summary_matches_payload(summary, payload) is False


def test_remote_summary_matches_eval_only_zero_task_starts():
    payload, summary = _summary_pair(
        task_starts=0,
        eval_only=True,
        eval_dir="official_eval_recovery",
    )

    assert runner.remote_summary_matches_payload(summary, payload) is True


def test_preexisting_owner_must_match_the_expected_runtime_tree(monkeypatch):
    owner = {
        "pid": 9876,
        "start_identity": "proc:12345",
        "owner_nonce": "e" * 32,
        "claim_sha256": "c" * 64,
        "invocation_id": "e" * 32,
        "runtime_tree_sha256": "b" * 64,
    }
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
            AssertionError("a stale runtime owner must never be adopted")
        ),
    )

    with pytest.raises(runner.RemoteRunnerUnavailable):
        runner.run_remote(_eval_only_args())


@pytest.mark.parametrize("mismatch", ["claim", "invocation"])
def test_periodic_probe_rejects_owner_identity_mismatch(monkeypatch, mismatch):
    class WaitingProcess:
        pid = 4321
        returncode = None

    fixed_uuid = SimpleNamespace(hex="e" * 32)
    owner = {
        "pid": 9876,
        "start_identity": "proc:12345",
        "owner_nonce": "e" * 32,
        "claim_sha256": "c" * 64,
        "invocation_id": "e" * 32,
    }
    owner["claim_sha256" if mismatch == "claim" else "invocation_id"] = (
        "d" * 64 if mismatch == "claim" else "f" * 32
    )
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(
        runner._transport_recovery,
        "runner_claim_sha256",
        lambda payload: "c" * 64,
    )
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: WaitingProcess(),
    )
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda proc, payload, timeout, poll_callback, **kwargs: poll_callback(),
    )
    monkeypatch.setattr(
        runner,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "dead",
            "runner_owner": owner,
            "summary": {"status": "done"},
        },
    )
    monkeypatch.setattr(
        runner._transport_recovery,
        "remote_summary_matches_payload",
        lambda summary, payload: True,
    )
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: ({"ok": True}, None),
    )

    with pytest.raises(RuntimeError, match="became unavailable"):
        runner.run_remote(_eval_only_args())


@pytest.mark.parametrize("mismatch", ["claim", "invocation"])
def test_primary_timeout_rejects_owner_identity_mismatch(monkeypatch, mismatch):
    class HangingProcess:
        pid = 4321
        returncode = None

    fixed_uuid = SimpleNamespace(hex="e" * 32)
    owner = {
        "pid": 9876,
        "start_identity": "proc:12345",
        "owner_nonce": "e" * 32,
        "claim_sha256": "c" * 64,
        "invocation_id": "e" * 32,
    }
    owner["claim_sha256" if mismatch == "claim" else "invocation_id"] = (
        "d" * 64 if mismatch == "claim" else "f" * 32
    )
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(
        runner._transport_recovery,
        "runner_claim_sha256",
        lambda payload: "c" * 64,
    )
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: HangingProcess(),
    )
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["ssh"], 30)
        ),
    )
    monkeypatch.setattr(
        runner,
        "probe_remote_execution_state",
        lambda **kwargs: {
            "runner_state": "dead",
            "runner_owner": owner,
            "summary": {"status": "done"},
        },
    )
    monkeypatch.setattr(
        runner._transport_recovery,
        "remote_summary_matches_payload",
        lambda summary, payload: True,
    )
    monkeypatch.setattr(
        runner,
        "_cleanup_remote_execution",
        lambda **kwargs: ({"ok": True}, None),
    )
    args = _eval_only_args()
    args.total_timeout = 30

    with pytest.raises(RuntimeError, match="timed out"):
        runner.run_remote(args)
