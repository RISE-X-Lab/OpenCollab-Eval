from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _remote_namespace,
    _seed_remote_completed_generation,
    pytest,
)


def _go_eval_row(task: str) -> dict:
    return {
        "instance_id": task,
        "fail_to_pass": ["pkg/feature_test.go::TestFeature"],
        "repo_language": "go",
    }


def test_eval_binding_exception_runs_owned_process_and_container_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A marker I/O exception after Popen must not leak the eval container."""
    namespace = _remote_namespace(tmp_path)
    task = "task-binding-exception"
    _seed_remote_completed_generation(namespace, task)

    class RunningProcess:
        pid = 424273

        def wait(self, timeout=None):
            return 1

    calls = []
    monkeypatch.setattr(
        namespace["subprocess"], "Popen", lambda *args, **kwargs: RunningProcess()
    )
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["bind_eval_container_marker"] = lambda *args, **kwargs: (
        (_ for _ in ()).throw(OSError("marker write failed"))
    )
    namespace["terminate_process_group_bounded"] = (
        lambda proc: calls.append(("process", proc.pid)) or True
    )
    namespace["cleanup_eval_container"] = (
        lambda *args: calls.append(("container", args[2])) or {"ok": True}
    )

    result = namespace["eval_for_task"](_go_eval_row(task))

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["container_binding"]["status"] == (
        "container_identity_binding_exception"
    )
    assert calls[0] == ("process", 424273)
    assert len([kind for kind, _name in calls if kind == "container"]) >= 1


def test_eval_binding_failure_restores_spawn_signal_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A failed marker bind must not leave later tasks under deferred signals."""
    namespace = _remote_namespace(tmp_path)
    task = "task-binding-signal-restore"
    _seed_remote_completed_generation(namespace, task)
    events = []
    state = object()
    namespace["block_spawn_signals"] = lambda: events.append("block") or state
    namespace["restore_spawn_signals"] = lambda value: events.append(
        ("restore", value)
    )
    namespace["bind_eval_container_marker"] = lambda *args, **kwargs: {
        "ok": False,
        "status": "container_identity_unavailable",
    }
    namespace["terminate_process_group_bounded"] = lambda proc: events.append(
        ("terminate", proc.pid)
    ) or True
    namespace["cleanup_temporary_output"] = lambda _temporary: []

    class RunningProcess:
        pid = 424274

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: RunningProcess(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True}

    result = namespace["eval_for_task_once"](_go_eval_row(task))

    assert result["status"] == "technical_eval_failed"
    assert events == ["block", ("terminate", 424274), ("restore", state)]
