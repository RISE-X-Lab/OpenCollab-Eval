from __future__ import annotations

import json
from dataclasses import replace

import pytest
from swe_v1_prolite_runner_test_support import _remote_namespace
from test_swe_g11_parallel_runner import _args, _load_module, _reusable_summary
from test_swe_v1_prolite_runner_eval import _owned_eval_marker

import opencollab_eval.commands.swe_v1_prolite_runner as prolite_runner


def test_parallel_default_is_forwarded_to_single_task_runner():
    module = _load_module()
    config = module.resolve_config(_args())

    assert config.eval_container_bind_timeout == 30
    command = module.task_command(config, 51)
    assert command[command.index("--eval-container-bind-timeout") + 1] == "30"


@pytest.mark.parametrize("value", [0, -1, 301])
def test_parallel_config_rejects_container_bind_timeout_outside_bound(value):
    module = _load_module()

    with pytest.raises(ValueError, match="eval-container-bind-timeout"):
        module.resolve_config(_args(eval_container_bind_timeout=value))


def test_container_bind_timeout_changes_report_reuse_identity():
    module = _load_module()
    config = replace(
        module.resolve_config(_args(start_index=1, end_index=1)),
        runtime_tree_sha256="a" * 64,
    )
    summary = _reusable_summary(config, 1)

    assert module.report_is_reusable(summary, config, 1) is True
    changed = replace(config, eval_container_bind_timeout=45)
    assert module.report_is_reusable(summary, changed, 1) is False


@pytest.mark.parametrize("value", [0, -1, 301])
def test_remote_config_rejects_container_bind_timeout_outside_bound(tmp_path, value):
    with pytest.raises(ValueError, match="eval_container_bind_timeout"):
        _remote_namespace(tmp_path, eval_container_bind_timeout=value)


@pytest.mark.parametrize("value", [0, 301])
def test_single_task_cli_rejects_container_bind_timeout_outside_bound(value):
    with pytest.raises(SystemExit) as exc:
        prolite_runner.main(argv=["--eval-container-bind-timeout", str(value)])

    assert exc.value.code == 2


def test_binding_accepts_cidfile_after_old_two_second_limit(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_container_bind_timeout=30)
    cidfile = tmp_path / "container.cid"
    marker = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-high-load-cid"
    _owned_eval_marker(namespace, marker, "", container_name, state="pending")
    clock = [0.0]

    class RunningProcess:
        @staticmethod
        def poll():
            return None

    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 2.1 and not cidfile.exists():
            cidfile.write_text("b" * 64 + "\n", encoding="ascii")

    monkeypatch.setattr(namespace["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(namespace["time"], "sleep", advance)

    result = namespace["bind_eval_container_marker"](
        cidfile,
        marker,
        container_name,
        RunningProcess(),
        timeout=namespace["eval_container_bind_timeout"],
    )

    assert clock[0] >= 2.1
    assert result == {"ok": True, "container_id": "b" * 64}
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "active"


def test_binding_fails_fast_when_docker_exits(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_container_bind_timeout=30)
    cidfile = tmp_path / "container.cid"
    marker = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-exited-before-cid"
    _owned_eval_marker(namespace, marker, "", container_name, state="pending")
    sleeps = []

    class ExitedProcess:
        @staticmethod
        def poll():
            return 125

    monkeypatch.setattr(namespace["time"], "sleep", sleeps.append)

    result = namespace["bind_eval_container_marker"](
        cidfile,
        marker,
        container_name,
        ExitedProcess(),
        timeout=namespace["eval_container_bind_timeout"],
    )

    assert result == {
        "ok": False,
        "status": "container_identity_unavailable",
        "details": "container cidfile did not appear",
    }
    assert sleeps == []
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "pending"


def test_binding_stops_after_configured_timeout(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, eval_container_bind_timeout=3)
    cidfile = tmp_path / "container.cid"
    marker = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-timeout"
    _owned_eval_marker(namespace, marker, "", container_name, state="pending")
    clock = [0.0]

    class RunningProcess:
        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(namespace["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        namespace["time"],
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    result = namespace["bind_eval_container_marker"](
        cidfile,
        marker,
        container_name,
        RunningProcess(),
        timeout=namespace["eval_container_bind_timeout"],
    )

    assert clock[0] >= 3
    assert result["status"] == "container_identity_unavailable"
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "pending"


def test_binding_rejects_symlink_cidfile(tmp_path):
    namespace = _remote_namespace(tmp_path, eval_container_bind_timeout=30)
    target = tmp_path / "foreign.cid"
    target.write_text("c" * 64 + "\n", encoding="ascii")
    cidfile = tmp_path / "container.cid"
    cidfile.symlink_to(target)
    marker = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-symlink-cid"
    _owned_eval_marker(namespace, marker, "", container_name, state="pending")

    class RunningProcess:
        @staticmethod
        def poll():
            return None

    result = namespace["bind_eval_container_marker"](
        cidfile,
        marker,
        container_name,
        RunningProcess(),
        timeout=namespace["eval_container_bind_timeout"],
    )

    assert result["ok"] is False
    assert result["status"] == "invalid_cidfile"
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "pending"


def test_binding_rejects_marker_with_foreign_owner(tmp_path):
    namespace = _remote_namespace(tmp_path, eval_container_bind_timeout=30)
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("d" * 64 + "\n", encoding="ascii")
    marker = tmp_path / "container.marker.json"
    container_name = "opencollab-prolite-foreign-owner"
    _owned_eval_marker(namespace, marker, "", container_name, state="pending")
    marker_value = json.loads(marker.read_text(encoding="utf-8"))
    marker_value["owner_nonce"] = "e" * 32
    marker.write_text(json.dumps(marker_value), encoding="utf-8")

    class RunningProcess:
        @staticmethod
        def poll():
            return None

    result = namespace["bind_eval_container_marker"](
        cidfile,
        marker,
        container_name,
        RunningProcess(),
        timeout=namespace["eval_container_bind_timeout"],
    )

    assert result["ok"] is False
    assert result["status"] == "invalid_marker_ownership"
    assert json.loads(marker.read_text(encoding="utf-8"))["state"] == "pending"
