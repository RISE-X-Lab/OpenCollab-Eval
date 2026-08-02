from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opencollab_eval.commands import _swe_eval_relay_health as relay_health

MODEL_ARGUMENTS = [
    "--model-name",
    "public-evaluation-model",
    "--llm-model",
    "provider/model",
]


def _load_entry_module() -> Any:
    module = importlib.import_module("opencollab_eval.commands.swe_eval_run")
    return importlib.reload(module)


def test_base_team_entry_delegates_to_parallel_runner(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "51,52",
            "--solver",
            "baseTeam",
            "--workers",
            "2",
            "--run-id",
            "base_team_smoke",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert "--workflow" in argv
    assert argv[argv.index("--workflow") + 1] == "base-team"
    assert "--max-workers" in argv
    assert argv[argv.index("--max-workers") + 1] == "2"
    assert "--dry-run" in argv


def test_team_pro_entry_uses_dynamic_workflow_defaults(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "7,11",
            "--solver",
            "TeamPro",
            "--workers",
            "2",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--workflow") + 1] == "team-pro"
    assert argv[argv.index("--budget") + 1] == "4000000"
    assert argv[argv.index("--max-task-starts") + 1] == "3"
    assert argv[argv.index("--model-name") + 1] == "public-evaluation-model"
    assert argv[argv.index("--llm-model") + 1] == "provider/model"
    assert argv[argv.index("--temperature") + 1] == "1.0"
    assert argv[argv.index("--top-p") + 1] == "1.0"
    assert argv[argv.index("--max-output-tokens") + 1] == "32768"
    assert "--workflow-env" not in argv


def test_team_pro_entry_accepts_explicit_runtime_settings(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)
    assert module.main(
        [
            "--indices",
            "7",
            "--solver",
            "TeamPro",
            "--budget=2000000",
            "--max-task-starts",
            "2",
            "--model-name",
            "custom-teampro",
            "--llm-model",
            "provider/custom",
            "--temperature",
            "0",
            "--workflow-env",
            "OPENCOLLAB_TOP_P=0",
            "--dry-run",
        ]
    ) == 0

    argv = captured["argv"]
    assert argv.count("--temperature") == 1
    assert argv[argv.index("--temperature") + 1] == "0"
    assert argv[argv.index("--model-name") + 1] == "custom-teampro"
    assert argv[argv.index("--llm-model") + 1] == "provider/custom"


def test_team_pro_entry_rejects_workflow_override(monkeypatch: Any) -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="selected by --solver"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "TeamPro",
                "--workflow",
                "validation-council-solve",
                "--dry-run",
            ]
        )


def test_unified_entry_rejects_historical_eval_only_maintenance_options() -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="single-task maintenance API"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "TeamPro",
                "--eval-only",
                "--parent-output-dir",
                "/tmp/parent",
            ]
        )


def test_openhands_entry_delegates_external_workflow(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "1-2",
            "--solver",
            "openhands",
            "--workers",
            "1",
            "--openhands-command",
            "openhands --help",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--workflow") + 1] == "openhands-external"
    assert argv[argv.index("--openhands-command") + 1] == "openhands --help"
    assert argv[argv.index("--max-task-starts") + 1] == "2"
    assert argv[argv.index("--budget") + 1] == "16000000"
    assert argv[argv.index("--llm-model") + 1] == "provider/model"


def test_openhands_entry_has_one_command_defaults(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)
    assert module.main(
        [
            "--indices",
            "51-100",
            "--solver",
            "openhands",
            "--workers",
            "5",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    ) == 0
    argv = captured["argv"]
    assert argv[argv.index("--openhands-command") + 1].endswith("--file {prompt_file}")
    assert argv[argv.index("--max-empty-patch-retries") + 1] == "1"
    assert argv[argv.index("--max-eval-attempts") + 1] == "2"
    assert argv[argv.index("--openhands-empty-patch-rejections") + 1] == "2"
    assert argv[argv.index("--max-steps") + 1] == "120"


@pytest.mark.parametrize("workers", [None, 2, 4])
def test_claude_code_entry_has_single_attempt_external_solver_defaults(
    monkeypatch: Any,
    workers: int | None,
) -> None:
    module = _load_entry_module()
    monkeypatch.setenv("OPENCOLLAB_SWE_LLM_PROVIDER", "openai")
    captured: dict[str, Any] = {}
    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0
    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)
    monkeypatch.setattr(module, "_require_claude_code_configuration", lambda **kwargs: None)
    assert module.main(
        [
            "--indices",
            "7",
            "--solver",
            "claude-code",
            "--model-name",
            "claude-code-2.1.175-glm-5.2",
            "--llm-model",
            "glm-5.2",
            *(["--workers", str(workers)] if workers is not None else []),
            "--dry-run",
        ]
    ) == 0
    argv = captured["argv"]
    assert argv[argv.index("--workflow") + 1] == "openhands-external"
    assert "run_claude_code_cli.sh" in argv[argv.index("--openhands-command") + 1]
    assert argv[argv.index("--max-task-starts") + 1] == "1"
    assert argv[argv.index("--max-empty-patch-retries") + 1] == "0"
    assert argv[argv.index("--max-eval-attempts") + 1] == "1"
    assert argv[argv.index("--openhands-empty-patch-rejections") + 1] == "0"
    assert argv[argv.index("--runner-attempts") + 1] == "1"
    assert argv[argv.index("--max-workers") + 1] == str(workers or 1)
    assert argv[argv.index("--context-window") + 1] == "200000"
    assert argv[argv.index("--llm-provider") + 1] == "anthropic"
@pytest.mark.parametrize(
    "arguments",
    [
        ["--workers", "0"],
        ["--workers", "5"],
        ["--context-window", "400000"],
        ["--max-task-starts", "2"],
        ["--max-eval-attempts", "2"],
        ["--runner-attempts", "2"],
        ["--max-empty-patch-retries", "1"],
        ["--openhands-empty-patch-rejections", "1"],
        ["--openhands-command", "untrusted-wrapper"],
        ["--llm-provider", "openai"],
        ["--max-eval-attempts", "1", "--max-eval-attempts", "2"],
        ["--context-window", "200000", "--context-window", "400000"],
        ["--llm-model", "other-model"],
    ],
)
def test_claude_code_entry_rejects_noncanonical_single_sample_settings(
    monkeypatch: Any,
    arguments: list[str],
) -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="claude-code requires"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "claude-code",
                "--model-name",
                "claude-code-2.1.175-glm-5.2",
                "--llm-model",
                "glm-5.2",
                *arguments,
                "--dry-run",
            ]
        )


@pytest.mark.parametrize(
    "option,value",
    [
        ("--model-name", "different-runtime"),
        ("--llm-model", "different-model"),
    ],
)
def test_claude_code_entry_rejects_noncanonical_model_identity(
    monkeypatch: Any,
    option: str,
    value: str,
) -> None:
    module = _load_entry_module()
    arguments = {
        "--model-name": "claude-code-2.1.175-glm-5.2",
        "--llm-model": "glm-5.2",
    }
    arguments[option] = value

    with pytest.raises(SystemExit, match="claude-code requires"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "claude-code",
                "--model-name",
                arguments["--model-name"],
                "--llm-model",
                arguments["--llm-model"],
                "--dry-run",
            ]
        )


def test_solver_entry_fails_fast_without_model_configuration(monkeypatch: Any) -> None:
    module = _load_entry_module()
    monkeypatch.delenv("OPENCOLLAB_SWE_MODEL_NAME", raising=False)
    monkeypatch.delenv("OPENCOLLAB_SWE_LLM_MODEL", raising=False)

    with pytest.raises(
        SystemExit,
        match="requires --model-name or OPENCOLLAB_SWE_MODEL_NAME",
    ):
        module.main(["--indices", "1", "--solver", "g11", "--dry-run"])


def test_solver_entry_reads_model_configuration_from_environment(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setenv("OPENCOLLAB_SWE_MODEL_NAME", "environment-run")
    monkeypatch.setenv("OPENCOLLAB_SWE_LLM_MODEL", "provider/environment-model")
    monkeypatch.setattr(module, "_load_module", lambda name: FakeRunner)

    assert module.main(["--indices", "1", "--solver", "g11", "--dry-run"]) == 0
    argv = captured["argv"]
    assert argv[argv.index("--model-name") + 1] == "environment-run"
    assert argv[argv.index("--llm-model") + 1] == "provider/environment-model"


def test_detached_plist_uses_module_entry_without_shell_wrapper() -> None:
    module = _load_entry_module()
    payload = module._launchd_plist(
        label="com.opencollab.eval.test",
        program_arguments=[
            sys.executable,
            "-m",
            "opencollab_eval.commands.swe_eval_run",
            "--indices",
            "51-100",
        ],
        stdout_path=Path("/tmp/stdout.log"),
        stderr_path=Path("/tmp/stderr.log"),
    )

    assert payload["ProgramArguments"][0] == sys.executable
    assert payload["ProgramArguments"][1:3] == [
        "-m",
        "opencollab_eval.commands.swe_eval_run",
    ]
    assert payload["KeepAlive"] is False
    assert "/bin/bash" not in payload["ProgramArguments"]


def test_detached_plist_preserves_explicit_pythonpath(monkeypatch: Any) -> None:
    module = _load_entry_module()
    monkeypatch.setenv("PYTHONPATH", "/workspace/eval/src:/workspace/opencollab")

    payload = module._launchd_plist(
        label="com.opencollab.eval.test",
        program_arguments=[sys.executable, "-m", "opencollab_eval.commands.swe_eval_run"],
        stdout_path=Path("/tmp/stdout.log"),
        stderr_path=Path("/tmp/stderr.log"),
    )

    assert payload["EnvironmentVariables"]["PYTHONPATH"] == (
        "/workspace/eval/src:/workspace/opencollab"
    )


def test_detached_plist_makes_relative_pythonpath_independent_of_child_cwd(
    monkeypatch: Any, tmp_path: Path
) -> None:
    module = _load_entry_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "src:../opencollab")

    payload = module._launchd_plist(
        label="com.opencollab.eval.test",
        program_arguments=[sys.executable, "-m", "opencollab_eval.commands.swe_eval_run"],
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert payload["EnvironmentVariables"]["PYTHONPATH"] == os.pathsep.join(
        (str(tmp_path / "src"), str(tmp_path.parent / "opencollab"))
    )


@pytest.mark.parametrize(
    ("value", "parts"),
    [
        (":src", (".", "src")),
        ("src::../opencollab", ("src", ".", "../opencollab")),
        ("src:", ("src", ".")),
    ],
)
def test_detached_plist_preserves_empty_pythonpath_components(
    monkeypatch: Any, tmp_path: Path, value: str, parts: tuple[str, ...]
) -> None:
    module = _load_entry_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", value)

    payload = module._launchd_plist(
        label="com.opencollab.eval.test",
        program_arguments=[sys.executable, "-m", "opencollab_eval.commands.swe_eval_run"],
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    expected = [str(Path(os.path.abspath(part))) for part in parts]
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == os.pathsep.join(expected)


def test_detach_only_removes_launch_options_from_child() -> None:
    module = _load_entry_module()

    assert module._without_launch_options(
        [
            "--indices",
            "51-100",
            "--detach",
            "--launchd-label",
            "com.example.eval",
            "--no-persistent-proxy",
        ]
    ) == ["--indices", "51-100", "--no-persistent-proxy"]


def test_persistent_proxy_fails_fast_without_remote_host(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_entry_module()
    monkeypatch.delenv("OPENCOLLAB_SWE_HOST", raising=False)

    with pytest.raises(RuntimeError, match="--host or OPENCOLLAB_SWE_HOST"):
        module._ensure_proxy_agent(output_dir=tmp_path, remaining=[])


def test_local_model_relay_launches_without_putting_api_key_in_plist(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_entry_module()
    written: list[dict[str, Any]] = []
    health = iter([False, True])
    monkeypatch.setattr(
        module,
        "_local_relay_healthy",
        lambda _url, _upstream, **_kwargs: next(health),
    )
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *args, **kwargs: SimpleNamespace(returncode=1 if args[0] == "print" else 0),
    )
    monkeypatch.setattr(module, "_write_plist", lambda _path, payload: written.append(payload))
    monkeypatch.setattr(module.shutil, "copy2", lambda _source, _target: None)

    result = module._ensure_local_proxy_agent(
        output_dir=tmp_path,
        remaining=[
            "--local-proxy-base-url",
            "http://127.0.0.1:8879",
            "--proxy-env-file",
            "/private/tmp/kimi.env",
            "--llm-timeout",
            "21600",
        ],
        upstream_base_url="https://api.kimi.com/coding/v1",
    )

    assert result["status"] == "started"
    program = written[0]["ProgramArguments"]
    assert "opencollab_eval.commands.llm_api_proxy" in program
    assert "--aggregate-chat-stream" in program
    assert program[program.index("--timeout") + 1] == "240.0"
    assert "/private/tmp/kimi.env" in program
    assert not any(argument.startswith("sk-") for argument in program)


def test_local_relay_health_rejects_another_upstream(monkeypatch: Any) -> None:
    module = _load_entry_module()
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "kind": "authenticated_model_relay",
                    "upstream_base_url_sha256": hashlib.sha256(
                        b"https://other.example/v1"
                    ).hexdigest(),
                }
            ).encode()

    monkeypatch.setattr(relay_health.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    assert module._local_relay_healthy(
        "http://127.0.0.1:8879", "https://api.kimi.com/coding/v1"
    ) is False


def test_remote_relay_health_binds_the_expected_upstream(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured = {}
    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(relay_health.subprocess, "run", fake_run)

    assert module._remote_proxy_healthy(
        ssh_command="ssh",
        host="host",
        base_url="http://127.0.0.1:18789/v1",
        upstream_base_url="https://api.kimi.com/coding/v1",
        relay_mode="responses-pass-through",
        compact_tool_schemas=False,
        max_upstream_request_bytes=0,
    )
    joined = " ".join(captured["command"])
    expected = hashlib.sha256(b"https://api.kimi.com/coding/v1").hexdigest()
    assert expected in joined
    assert "upstream_base_url_sha256" in joined
    assert "http://127.0.0.1:18789/healthz" in joined
    assert "http://127.0.0.1:18789/v1/healthz" not in joined
    assert "responses-pass-through" in joined
    assert subprocess.run(["sh", "-n", "-c", captured["command"][-1]], check=False).returncode == 0


def test_remote_relay_socket_health_requires_private_bound_socket(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured = {}
    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(relay_health.subprocess, "run", fake_run)
    socket_path = "/tmp/opencollab-llmproxy-18790.sock"
    assert module._remote_proxy_socket_healthy(
        ssh_command="ssh",
        host="host",
        socket_path=socket_path,
        upstream_base_url="https://api.example.invalid/v1",
        relay_mode="responses-pass-through",
        compact_tool_schemas=False,
        max_upstream_request_bytes=0,
    )
    joined = " ".join(captured["command"])
    remote_command = captured["command"][-1]
    assert socket_path in joined
    assert "stat.S_ISSOCK" in joined
    assert "stat.S_IMODE(mode) & 0o077 == 0" in joined
    assert "http.client.parse_headers" in joined
    assert "stream.read(length)" in joined
    assert "responses-pass-through" in joined
    assert hashlib.sha256(b"https://api.example.invalid/v1").hexdigest() in joined
    assert subprocess.run(["sh", "-n", "-c", remote_command], check=False).returncode == 0


def test_failed_persistent_relay_start_is_unloaded(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    launchctl_calls: list[tuple[str, ...]] = []

    def fake_launchctl(*arguments: str, **_kwargs: Any) -> SimpleNamespace:
        launchctl_calls.append(arguments)
        return SimpleNamespace(returncode=1 if arguments[0] == "print" else 0)

    monkeypatch.setattr(module, "_ensure_local_proxy_agent", lambda **_kwargs: {})
    monkeypatch.setattr(module, "_remote_proxy_healthy", lambda **_kwargs: False)
    monkeypatch.setattr(module, "_remove_stale_remote_proxy_socket", lambda **_kwargs: None)
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_launchctl", fake_launchctl)
    monkeypatch.setattr(module, "_write_plist", lambda *_args: None)
    monkeypatch.setattr(module.shutil, "copy2", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="failed health check"):
        module._ensure_proxy_agent(
            output_dir=tmp_path,
            remaining=["--host", "host"],
            upstream_base_url="https://api.example.invalid/v1",
        )

    target = f"gui/{os.getuid()}/com.opencollab.proxy.host.18788"
    assert ("bootout", target) in launchctl_calls


def test_foreground_entry_starts_model_relay_before_runner(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    calls = []
    monkeypatch.setattr(
        module,
        "_ensure_proxy_agent",
        lambda **kwargs: calls.append(("proxy", kwargs)),
    )
    monkeypatch.setattr(
        module,
        "_run_parallel_runner",
        lambda args, remaining: calls.append(("runner", args, remaining)) or 0,
    )

    result = module.main(
        [
            "--indices",
            "1",
            "--solver",
            "g11",
            "--run-id",
            "foreground",
            "--output-dir",
            str(tmp_path),
            "--proxy-upstream-base-url",
            "https://api.kimi.com/coding/v1",
            "--host",
            "host",
        ]
    )

    assert result == 0
    assert [call[0] for call in calls] == ["proxy", "runner"]
    assert calls[0][1]["relay_mode"] == "aggregate-chat-stream"


def test_detach_starts_direct_launch_agent_once(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    written: list[tuple[Path, dict[str, Any]]] = []
    launchctl_calls: list[tuple[str, ...]] = []

    def fake_launchctl(*arguments: str, check: bool = False) -> SimpleNamespace:
        launchctl_calls.append(arguments)
        return SimpleNamespace(returncode=1 if arguments[0] == "print" else 0, stdout="", stderr="")

    monkeypatch.setattr(module, "_launchctl", fake_launchctl)
    monkeypatch.setattr(module, "_write_plist", lambda path, payload: written.append((path, payload)))
    monkeypatch.setattr(module.shutil, "copy2", lambda source, target: None)

    rc = module.main(
        [
            "--indices",
            "51-100",
            "--solver",
            "openhands",
            "--workers",
            "5",
            "--run-id",
            "openhands_51_100",
            "--output-dir",
            str(tmp_path),
            "--detach",
            "--no-persistent-proxy",
        ]
    )

    assert rc == 0
    assert len(written) == 1
    program = written[0][1]["ProgramArguments"]
    assert program[:3] == [
        sys.executable,
        "-m",
        "opencollab_eval.commands.swe_eval_run",
    ]
    assert "--detach" not in program
    assert "--no-persistent-proxy" in program
    assert any(call[0] == "bootstrap" for call in launchctl_calls)


def test_kimi_remote_api_file_skips_persistent_proxy(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    monkeypatch.setattr(
        module,
        "_ensure_proxy_agent",
        lambda **_kwargs: pytest.fail("Kimi direct mode must not start a proxy"),
    )
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *arguments, **_kwargs: SimpleNamespace(
            returncode=1 if arguments[0] == "print" else 0,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr(module, "_write_plist", lambda *_args: None)
    monkeypatch.setattr(module.shutil, "copy2", lambda *_args: None)

    assert module.main(
        [
            "--indices", "1", "--solver", "g11",
            "--model-name", "kimi-k2.7-code-g11",
            "--llm-model", "kimi-for-coding", "--llm-provider", "openai",
            "--remote-proxy-base-url", "https://api.kimi.com/coding/v1",
            "--output-dir", str(tmp_path), "--detach",
            "--remote-api-env-file", "/srv/opencollab/secrets/kimi.env",
        ]
    ) == 0


def test_k3_remote_api_file_skips_persistent_proxy(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    monkeypatch.setattr(
        module,
        "_ensure_proxy_agent",
        lambda **_kwargs: pytest.fail("Kimi direct mode must not start a proxy"),
    )
    monkeypatch.setattr(
        module,
        "_launchctl",
        lambda *arguments, **_kwargs: SimpleNamespace(
            returncode=1 if arguments[0] == "print" else 0,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr(module, "_write_plist", lambda *_args: None)
    monkeypatch.setattr(module.shutil, "copy2", lambda *_args: None)

    assert module.main(
        [
            "--indices", "7", "--solver", "g11",
            "--model-name", "kimi-k3-g11",
            "--llm-model", "k3", "--llm-provider", "openai",
            "--remote-proxy-base-url", "https://api.kimi.com/coding/v1",
            "--output-dir", str(tmp_path), "--detach",
            "--remote-api-env-file", "/srv/opencollab/secrets/kimi.env",
        ]
    ) == 0
