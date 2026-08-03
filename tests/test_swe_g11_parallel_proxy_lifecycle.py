"""Run-level proxy ownership for the parallel G11 evaluator."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_swe_g11_parallel_runner import _args, _load_module


def test_parallel_parent_owns_proxy_before_preflight(monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args())
    calls = []
    monkeypatch.setattr(
        module,
        "ensure_remote_proxy",
        lambda **kwargs: calls.append(kwargs) or {
            "status": "started_fallback_port",
            "remote_proxy_base_url": "http://127.0.0.1:18801",
        },
    )

    effective, summary, owned = module.prepare_run_proxy(config)

    assert calls[0]["enabled"] is True
    assert calls[0]["ssh_command"][-10:] == [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o",
        "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o",
        "TCPKeepAlive=yes",
    ]
    assert summary["status"] == "started_fallback_port"
    assert owned is True
    assert effective.remote_proxy_base_url == "http://127.0.0.1:18801"
    assert effective.no_ensure_remote_proxy is True


def test_parallel_parent_skips_proxy_for_direct_remote_api(monkeypatch):
    module = _load_module()
    config = module.resolve_config(
        _args(
            llm_model="kimi-for-coding",
            llm_provider="openai",
            remote_api_env_file="/remote/kimi.env",
            remote_proxy_base_url="https://api.kimi.com/coding/v1",
            local_proxy_base_url="",
            context_window=262144,
            top_p=0.95,
        )
    )
    monkeypatch.setattr(
        module,
        "ensure_remote_proxy",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("proxy must be skipped")),
    )

    effective, summary, owned = module.prepare_run_proxy(config)

    assert effective is config
    assert summary == {"status": "direct_remote_api"}
    assert owned is False


def test_run_parallel_cleans_parent_owned_proxy(monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args())
    events = []
    effective = replace(config, no_ensure_remote_proxy=True)
    monkeypatch.setattr(module._parallel_process, "clear_interrupted", lambda: events.append("clear"))
    monkeypatch.setattr(module._parallel_process, "install_signal_handlers", lambda: "handlers")
    monkeypatch.setattr(
        module._parallel_process,
        "terminate_active_task_groups",
        lambda: events.append("tasks"),
    )
    monkeypatch.setattr(
        module._parallel_process,
        "restore_signal_handlers",
        lambda value: events.append(value),
    )
    monkeypatch.setattr(
        module,
        "prepare_run_proxy",
        lambda _config: (effective, {"status": "started"}, True),
    )
    monkeypatch.setattr(
        module,
        "_run_parallel",
        lambda received, *, proxy_summary: events.append((received, proxy_summary))
        or {"status": "done"},
    )
    monkeypatch.setattr(module, "cleanup_remote_proxy_tunnels", lambda: events.append("proxy"))

    assert module.run_parallel(config) == {"status": "done"}
    assert events == [
        "clear",
        (effective, {"status": "started"}),
        "tasks",
        "proxy",
        "handlers",
        "clear",
    ]


def test_run_parallel_cleans_parent_owned_proxy_after_failure(monkeypatch):
    module = _load_module()
    config = module.resolve_config(_args())
    events = []
    effective = replace(config, no_ensure_remote_proxy=True)
    monkeypatch.setattr(module._parallel_process, "clear_interrupted", lambda: None)
    monkeypatch.setattr(module._parallel_process, "install_signal_handlers", lambda: "handlers")
    monkeypatch.setattr(
        module._parallel_process,
        "set_interrupted",
        lambda: events.append("interrupted"),
    )
    monkeypatch.setattr(
        module._parallel_process,
        "terminate_active_task_groups",
        lambda: events.append("tasks"),
    )
    monkeypatch.setattr(
        module._parallel_process,
        "restore_signal_handlers",
        lambda value: events.append(value),
    )
    monkeypatch.setattr(
        module,
        "prepare_run_proxy",
        lambda _config: (effective, {"status": "started"}, True),
    )

    def fail_run(_config, *, proxy_summary):
        assert proxy_summary == {"status": "started"}
        raise RuntimeError("preflight failed")

    monkeypatch.setattr(module, "_run_parallel", fail_run)
    monkeypatch.setattr(module, "cleanup_remote_proxy_tunnels", lambda: events.append("proxy"))

    with pytest.raises(RuntimeError, match="preflight failed"):
        module.run_parallel(config)
    assert events == ["interrupted", "tasks", "proxy", "handlers"]


def test_preflight_runtime_identity_reaches_task_reuse_checks(tmp_path, monkeypatch):
    module = _load_module()
    config = replace(
        module.resolve_config(_args(output_dir=tmp_path, indices="8")),
        no_ensure_remote_proxy=True,
    )
    received = []
    incomplete = {
        "index": 8,
        "returncode": 1,
        "runner_status": "missing_report",
        "completed": False,
        "failure_scope": "task",
        "failure_probe": {},
    }

    def capture(per_task_config, _index):
        received.append(per_task_config)
        return incomplete

    monkeypatch.setattr(module, "prepare_runtime", lambda _config: "a" * 64)
    monkeypatch.setattr(module, "run_remote_health_checks", lambda _config: {})
    monkeypatch.setattr(module, "run_remote_model_probe", lambda _config: {})
    monkeypatch.setattr(module, "run_one", capture)
    monkeypatch.setattr(
        module,
        "confirm_shared_runtime_after_task_failure",
        lambda _config, result: result,
    )
    monkeypatch.setattr(module, "systemic_failure_reasons", lambda _result: [])
    monkeypatch.setattr(module, "build_token_summary", lambda _config: {})
    monkeypatch.setattr(module, "save_progress", lambda *args, **kwargs: None)

    module.run_parallel(config)

    assert len(received) == 1
    assert received[0].runtime_tree_sha256 == "a" * 64
    assert received[0].no_sync_runtime is True
    assert received[0].no_ensure_remote_proxy is True
