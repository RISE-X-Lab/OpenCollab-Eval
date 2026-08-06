from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

from swe_v1_prolite_runner_test_support import (
    SimpleNamespace,
    os,
    pytest,
    runner,
    shlex,
    subprocess,
    sys,
)

from opencollab_eval.engine.swe_v1_remote_cleanup import process_start_identity


def _run_runtime_sync_command_locally(command, *, timeout=120, input_text=None):
    if command[0] == "rsync":
        destination = Path(command[-1].split(":", 1)[1])
        shutil.copy2(command[-2], destination)
        return subprocess.CompletedProcess(command, 0, "", "")
    assert command[:2] == ["ssh", "remote-host"]
    result = subprocess.run(
        ["sh", "-c", command[-1]],
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return result


def test_remote_probe_imports_only_from_the_synced_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    source_root = Path(os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).parents[1]))
    source = source_root / "src" / "opencollab_eval"
    shutil.copytree(source, runtime / "src" / "opencollab_eval")
    opencollab_root = Path(
        os.environ.get("OPENCOLLAB_SOURCE_ROOT", source_root.parent / "OpenCollab")
    )
    shutil.copytree(
        opencollab_root / "opencollab",
        runtime / "src" / "opencollab",
    )
    base = tmp_path / "run"
    base.mkdir()
    nonce = "a" * 32
    identity = process_start_identity(os.getpid(), [])
    (base / "runner.pid").write_text(
        json.dumps(
            {
                "schema": "opencollab.prolite_runner_owner.v1",
                "pid": os.getpid(),
                "start_identity": identity,
                "owner_nonce": nonce,
            }
        ),
        encoding="utf-8",
    )
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    real_run = subprocess.run

    def run_probe(command, **kwargs):
        assert command[:2] == ["ssh", "remote-host"]
        assert f"PYTHONPATH={runtime / 'src'}" in command[-1]
        return real_run(
            ["sh", "-c", command[-1]],
            cwd=empty_cwd,
            env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"},
            text=True,
            capture_output=True,
            timeout=kwargs["timeout"],
            check=False,
        )

    monkeypatch.setattr(runner.subprocess, "run", run_probe)

    assert runner.probe_remote_execution_state(
        ssh_command=["ssh"],
        host="remote-host",
        base_run_dir=str(base),
        remote_runtime_repo=str(runtime),
        owner_nonce=nonce,
    )["runner_state"] == "alive"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--budget", "-1"),
        ("--max-steps", "0"),
        ("--swe-timeout", "0"),
        ("--task-wall-timeout", "-2"),
        ("--eval-timeout", "0"),
        ("--llm-timeout", "0"),
        ("--provider-error-time-budget", "-1"),
        ("--total-timeout", "-3"),
        ("--checkpoint-interval", "-1"),
        ("--limit", "1001"),
        ("--run-id", "../../escape"),
    ],
)
def test_main_rejects_invalid_numeric_limits_before_dry_run(
    monkeypatch, option, value
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swe_v1_prolite_runner.py", "--dry-run", option, value],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2


def test_main_rejects_invalid_expected_runtime_tree_sha256(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swe_v1_prolite_runner.py",
            "--dry-run",
            "--expected-runtime-tree-sha256",
            "ABC123",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2


def test_main_requires_shared_preflight_identity_when_runtime_sync_is_disabled(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swe_v1_prolite_runner.py", "--dry-run", "--no-sync-runtime"],
    )

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2


def test_no_sync_runtime_verifies_shared_preflight_identity(monkeypatch):
    expected = "a" * 64
    observed = {"sha256": expected, "files": 42, "bytes": 1024}
    captured = {}

    def fake_verify(**kwargs):
        captured.update(kwargs)
        return observed

    monkeypatch.setattr(runner, "verify_remote_runtime", fake_verify)
    args = SimpleNamespace(
        no_sync_runtime=True,
        expected_runtime_tree_sha256=expected,
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
        remote_python="/remote/venv/bin/python",
    )

    summary = runner.prepare_runtime_summary(
        args,
        ["ssh"],
        eval_only=False,
    )

    assert captured == {
        "ssh_command": ["ssh"],
        "host": "remote-host",
        "remote_runtime_repo": "/remote/runtime",
        "expected": None,
        "remote_python": "/remote/venv/bin/python",
    }
    assert summary == {
        "source_tree": {
            "local": observed,
            "remote": observed,
            "verified": True,
        }
    }


@pytest.mark.parametrize("eval_only", [False, True])
def test_runtime_preparation_forwards_the_selected_remote_python(
    monkeypatch,
    eval_only,
):
    captured = {}

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {"source_tree": {"verified": True}}

    monkeypatch.setattr(runner, "sync_runtime", fake_sync)
    args = SimpleNamespace(
        no_sync_runtime=False,
        expected_runtime_tree_sha256="",
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
        remote_python="/remote/venv/bin/python",
    )

    summary = runner.prepare_runtime_summary(args, ["ssh"], eval_only=eval_only)

    assert captured == {
        "ssh_command": ["ssh"],
        "host": "remote-host",
        "remote_runtime_repo": "/remote/runtime",
        "remote_python": "/remote/venv/bin/python",
    }
    assert summary == {"source_tree": {"verified": True}}


@pytest.mark.parametrize("eval_only", [False, True])
def test_no_sync_runtime_without_shared_preflight_identity_fails_closed(
    monkeypatch,
    eval_only,
):
    calls = []
    monkeypatch.setattr(runner, "verify_remote_runtime", lambda **kwargs: calls.append(kwargs))
    args = SimpleNamespace(
        no_sync_runtime=True,
        expected_runtime_tree_sha256="",
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    with pytest.raises(RuntimeError, match="requires --expected-runtime-tree-sha256"):
        runner.prepare_runtime_summary(args, ["ssh"], eval_only=eval_only)

    assert calls == []


def test_no_sync_runtime_rejects_shared_preflight_identity_drift(monkeypatch):
    monkeypatch.setattr(
        runner,
        "verify_remote_runtime",
        lambda **kwargs: {"sha256": "b" * 64, "files": 42, "bytes": 1024},
    )
    args = SimpleNamespace(
        no_sync_runtime=True,
        expected_runtime_tree_sha256="a" * 64,
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    with pytest.raises(RuntimeError, match="shared preflight"):
        runner.prepare_runtime_summary(args, ["ssh"], eval_only=False)


@pytest.mark.parametrize(
    ("missing_default", "expected_option"),
    [
        ("DEFAULT_HOST", "--host"),
        ("DEFAULT_REMOTE_ROOT", "--remote-root"),
        ("DEFAULT_MODEL_NAME", "--model-name"),
        ("DEFAULT_SESSION_PREFIX", "--session-prefix"),
        ("DEFAULT_IMAGE_REPOSITORY", "--image-repository"),
        ("DEFAULT_REMOTE_PROXY_BASE_URL", "--remote-proxy-base-url"),
        ("DEFAULT_LOCAL_PROXY_BASE_URL", "--local-proxy-base-url"),
    ],
)
def test_main_rejects_missing_runtime_configuration_before_start(
    monkeypatch,
    capsys,
    missing_default,
    expected_option,
):
    defaults = {
        "DEFAULT_HOST": "remote-host",
        "DEFAULT_REMOTE_ROOT": "/remote/root",
        "DEFAULT_MODEL_NAME": "model",
        "DEFAULT_SESSION_PREFIX": "session",
        "DEFAULT_IMAGE_REPOSITORY": "registry.example/swebench",
        "DEFAULT_REMOTE_PROXY_BASE_URL": "http://127.0.0.1:18788",
        "DEFAULT_LOCAL_PROXY_BASE_URL": "http://127.0.0.1:8080",
    }
    for name, value in defaults.items():
        monkeypatch.setattr(runner, name, value)
    monkeypatch.setattr(runner, missing_default, "")
    monkeypatch.setattr(
        runner,
        "run_remote",
        lambda args: pytest.fail("run_remote must not start after parser validation fails"),
    )
    monkeypatch.setattr(sys, "argv", ["swe_v1_prolite_runner.py", "--dry-run"])

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    assert expected_option in capsys.readouterr().err


def test_run_remote_uses_installed_remote_module_without_inline_payload(monkeypatch):
    commands = []
    proxy_calls = []

    class FinishedProcess:
        pid = 424280
        returncode = 0

    def fake_popen(command, *args, **kwargs):
        commands.append(command)
        return FinishedProcess()

    monkeypatch.setattr(
        runner,
        "ensure_remote_proxy",
        lambda **kwargs: (
            proxy_calls.append(kwargs)
            or {"remote_proxy_base_url": "http://127.0.0.1:18788"}
        ),
    )
    monkeypatch.setattr(runner, "get_proxy_token", lambda path: "token")
    monkeypatch.setattr(
        runner,
        "verify_remote_runtime",
        lambda **kwargs: {"sha256": "a" * 64, "file_count": 1, "source_bytes": 1},
    )
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner, "_block_local_spawn_signals", lambda: object())
    monkeypatch.setattr(runner, "_restore_local_spawn_signals", lambda state: None)
    monkeypatch.setattr(
        runner,
        "_bounded_remote_communicate",
        lambda proc, payload, timeout, **kwargs: ('{"status":"done"}', ""),
    )
    monkeypatch.setattr(runner, "_local_process_group_exists", lambda pgid: False)
    args = SimpleNamespace(
        ssh_command="ssh -p 22",
        host="remote-host",
        local_proxy_base_url="http://127.0.0.1:8080",
        remote_proxy_base_url="http://127.0.0.1:18788",
        no_ensure_remote_proxy=False,
        no_sync_runtime=True,
        expected_runtime_tree_sha256="a" * 64,
        remote_runtime_repo="/remote/repo",
        remote_python="/remote/venv with space/bin/python",
        proxy_env_file=None,
        remote_root="/remote/root",
        base_run_dir="/remote/run",
        workflow="validation-council-solve",
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
        checkpoint_interval=300,
        max_task_starts=1,
        dry_run=False,
        total_timeout=30,
    )

    summary = runner.run_remote(args)

    assert summary["status"] == "done"
    assert commands[0][:2] == ["ssh", "-p"]
    for option in (
        "BatchMode=yes",
        "ConnectTimeout=20",
        "ServerAliveInterval=30",
        "ServerAliveCountMax=3",
        "TCPKeepAlive=yes",
    ):
        assert option in commands[0]
    remote_command = commands[0][-1]
    assert (
        "'/remote/venv with space/bin/python' "
        "-m opencollab_eval.engine.swe_v1_remote_runner"
    ) in remote_command
    assert proxy_calls[0]["remote_python"] == "/remote/venv with space/bin/python"
    assert "base64" not in remote_command
    assert "exec(" not in remote_command


def test_runtime_archive_imports_generation_entrypoints_from_clean_directory(monkeypatch, tmp_path):
    extracted = tmp_path / "clean-runtime"
    extracted.mkdir()
    imported = False

    def fake_run_checked(command, *, timeout=120, input_text=None):
        nonlocal imported
        if command[0] == "rsync":
            archive_path = command[-2]
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import opencollab, opencollab_eval.generation.gen_prediction; "
                    "import opencollab_eval.generation.gen_prediction_workflow; "
                    "print(opencollab.__file__)",
                ],
                cwd=extracted,
                env={**os.environ, "PYTHONPATH": str(extracted / "src")},
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            assert probe.returncode == 0, probe.stderr
            assert str(extracted / "src" / "opencollab") in probe.stdout
            imported = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", fake_run_checked)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
    )

    assert imported is True
    assert "src/opencollab_eval" in summary["synced_dirs"]
    assert "src/opencollab" in summary["synced_dirs"]
    assert summary["opencollab"]["public_api_version"] == 1
    assert (extracted / "src" / "opencollab" / "workflows.py").is_file()
    for name in (
        "gen_prediction_agent.py",
        "gen_prediction_config.py",
        "gen_prediction_constants.py",
        "gen_prediction_docker.py",
        "gen_prediction_patch.py",
        "gen_prediction_pending.py",
        "gen_prediction_safe_output.py",
        "gen_prediction_snapshot_container.py",
    ):
        assert (extracted / "src" / "opencollab_eval" / "generation" / name).is_file()
    assert (extracted / "src" / "opencollab_eval" / "engine" / "workspace_integrity.py").is_file()

    fifo_shell = (extracted / "src" / "opencollab_eval" / "resources" / "run_swe_v2_one_from_fifo.sh").read_text()
    openhands_shell = (extracted / "src" / "opencollab_eval" / "resources" / "run_openhands_cli.sh").read_text()
    assert "opencollab_eval.generation.gen_prediction" in fifo_shell
    assert "opencollab_eval.generation.openhands_runtime" in openhands_shell


def test_runtime_sync_rejects_an_incomplete_public_api_before_transfer(monkeypatch):
    calls = []
    real_import = runner.importlib.import_module

    def reject_missing_sdk_module(name):
        if name == "opencollab_eval.generation.gen_prediction_openhands":
            raise ModuleNotFoundError("No module named 'opencollab.tools'")
        return real_import(name)

    monkeypatch.setattr(runner.importlib, "import_module", reject_missing_sdk_module)
    monkeypatch.setattr(runner, "run_checked", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RuntimeError, match="public API is incompatible.*opencollab.tools"):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert calls == []


def test_runtime_sync_uses_the_selected_remote_python(monkeypatch):
    commands = []

    def run_checked(command, *, timeout=120, input_text=None):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run_checked", run_checked)
    selected = "/remote/venv with space/bin/python"

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
        remote_python=selected,
    )

    install_command = commands[-1][-1]
    quoted = shlex.quote(selected)
    assert quoted + " -m compileall -q " in install_command
    assert install_command.count("PYTHONPATH=src " + quoted + " -c ") == 2
    assert "PYTHONPATH=src python3 -c " not in install_command
    assert summary["remote_python"] == selected


def test_runtime_sync_requires_every_declared_input(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SYNC_FILES", ["missing.py"])
    monkeypatch.setattr(runner, "SYNC_DIRS", ["missing-package"])
    monkeypatch.setattr(runner, "run_checked", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(RuntimeError, match="missing-package, missing.py"):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo="/remote/runtime",
        )

    assert calls == []


def test_runtime_sync_replaces_reused_directory_with_manifested_snapshot(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "remote-runtime"
    remote_runtime.mkdir()
    (remote_runtime / "runtime.tgz").write_bytes(b"legacy runtime marker")
    (remote_runtime / "stale_module.py").write_text("STALE = True\n", encoding="utf-8")

    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)

    summary = runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo=str(remote_runtime),
    )

    assert not (remote_runtime / "stale_module.py").exists()
    manifest = json.loads((remote_runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert manifest["synced"] == summary["synced"]
    assert manifest["synced_dirs"] == summary["synced_dirs"]
    assert manifest["opencollab"] == summary["opencollab"]
    assert manifest["version"] == 2
    assert manifest["source_tree"] == summary["source_tree"]["local"]
    assert summary["source_tree"]["remote"] == summary["source_tree"]["local"]
    assert summary["source_tree"]["verified"] is True
    assert runner.verify_runtime_manifest(remote_runtime) == manifest["source_tree"]
    assert "src/opencollab_eval/generation/gen_prediction.py" in manifest["archive_members"]
    assert "src/opencollab/workflows.py" in manifest["archive_members"]
    assert not list(tmp_path.glob("remote-runtime.*"))


def test_runtime_manifest_rejects_remote_source_drift(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "remote-runtime"
    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)
    runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo=str(remote_runtime),
    )

    changed = remote_runtime / "src" / "opencollab_eval" / "__init__.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nDRIFT = True\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match"):
        runner.verify_runtime_manifest(remote_runtime)

    with pytest.raises(RuntimeError, match="does not match"):
        runner.verify_remote_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo=str(remote_runtime),
            expected=runner.runtime_tree_identity(
                remote_runtime,
                json.loads((remote_runtime / "runtime-manifest.json").read_text())["archive_members"],
            )
            | {"sha256": "0" * 64},
        )


def test_remote_runtime_verification_uses_long_idempotent_retry(monkeypatch):
    calls = []

    def run_ssh_checked(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"schema": "opencollab.runtime_tree.v1", "sha256": "a" * 64}),
            "",
        )

    monkeypatch.setattr(runner, "run_ssh_checked", run_ssh_checked)

    observed = runner.verify_remote_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo="/remote/runtime",
        expected=None,
    )

    assert observed["sha256"] == "a" * 64
    assert calls[0][1]["attempts"] == 30
    assert calls[0][1]["idempotent"] is True


def test_remote_runtime_rejects_a_hash_valid_incomplete_public_api(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "remote-runtime"
    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)
    runner.sync_runtime(
        ssh_command=["ssh"],
        host="remote-host",
        remote_runtime_repo=str(remote_runtime),
    )

    manifest_path = remote_runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = "src/opencollab/tools.py"
    manifest["archive_members"].remove(missing)
    (remote_runtime / missing).unlink()
    manifest["source_tree"] = runner.runtime_tree_identity(remote_runtime, manifest["archive_members"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing required modules: tools"):
        runner.verify_remote_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo=str(remote_runtime),
            expected=None,
        )


def test_runtime_sync_refuses_to_replace_unmarked_directory(monkeypatch, tmp_path):
    remote_runtime = tmp_path / "user-directory"
    remote_runtime.mkdir()
    user_file = remote_runtime / "notes.txt"
    user_file.write_text("keep me\n", encoding="utf-8")

    monkeypatch.setattr(runner, "run_checked", _run_runtime_sync_command_locally)

    with pytest.raises(RuntimeError, match="unmarked runtime directory"):
        runner.sync_runtime(
            ssh_command=["ssh"],
            host="remote-host",
            remote_runtime_repo=str(remote_runtime),
        )

    assert user_file.read_text(encoding="utf-8") == "keep me\n"
    assert not list(tmp_path.glob("user-directory.*"))


def test_proxy_tunnel_registers_before_pending_interrupt_restore(monkeypatch):
    process = SimpleNamespace(pid=424253, poll=lambda: None)
    cleanup_seen = []
    real_restore = runner._restore_local_spawn_signals

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert process in runner.REMOTE_PROXY_TUNNELS
        raise SystemExit(79)

    def fake_terminate(proc):
        cleanup_seen.append(proc)
        return True

    monkeypatch.setattr(runner, "_restore_local_spawn_signals", restore_then_interrupt)
    monkeypatch.setattr(runner, "terminate_local_process_group", fake_terminate)

    with pytest.raises(SystemExit) as exc:
        runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

    assert exc.value.code == 79
    assert cleanup_seen == [process]
    assert process not in runner.REMOTE_PROXY_TUNNELS


def test_proxy_tunnel_normal_leader_exit_cleanup_failure_is_explicit(monkeypatch):
    class ReapedTunnelLeader:
        pid = 424265
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return "", "ssh exited"

    process = ReapedTunnelLeader()
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        runner,
        "_ensure_local_process_group_quiesced_after_wait",
        lambda proc: False,
    )
    try:
        tunnel, message = runner.start_remote_proxy_tunnel(["ssh", "-N", "host"])

        assert tunnel is None
        assert "residual process-group descendants" in message
        assert process in runner.REMOTE_PROXY_TUNNELS
    finally:
        if process in runner.REMOTE_PROXY_TUNNELS:
            runner.REMOTE_PROXY_TUNNELS.remove(process)


def test_get_proxy_token_process_lookup_timeout_is_bounded(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "token_from_values", lambda values: "")

    def fake_check_output(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "check_output", fake_check_output)

    with pytest.raises(RuntimeError, match="timed out while locating"):
        runner.get_proxy_token(tmp_path / "missing.env")

    assert calls[0][1]["timeout"] == runner.PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_proxy_env_reader_rejects_unsafe_file_without_blocking(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    path = tmp_path / "proxy.env"
    if kind == "symlink":
        target = tmp_path / "real.env"
        target.write_text("GLM_PROXY_CLIENT_TOKEN=secret\n", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises((OSError, RuntimeError)):
        runner.load_shell_env(path)


def test_proxy_env_reader_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_PROXY_ENV_BYTES", 32)
    path = tmp_path / "proxy.env"
    path.write_text("GLM_PROXY_CLIENT_TOKEN=" + "x" * 64, encoding="utf-8")

    with pytest.raises(RuntimeError, match="bounded regular file"):
        runner.load_shell_env(path)
