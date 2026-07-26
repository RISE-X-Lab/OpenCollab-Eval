#!/usr/bin/env python3
"""Unified SWE evaluation entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from opencollab_eval.engine.solver_backend import (
    DEFAULT_WORKFLOW_SOLVERS,
    KIMI_CODING_BASE_URL,
    workflow_solver_spec,
)
from opencollab_eval.generation.claude_code_sidecar import relay_socket_path
from opencollab_eval.usage import model_context_window

from .ssh_reverse_proxy import remove_stale_remote_socket

WORKSPACE_ROOT = Path(
    os.environ.get("OPENCOLLAB_EVAL_WORKSPACE", Path.cwd())
).resolve()


def _load_module(name: str):
    return importlib.import_module(f"opencollab_eval.commands.{name}")


def _normalize_indices(args: argparse.Namespace) -> list[str]:
    if args.indices:
        return [part.strip() for part in args.indices.split(",") if part.strip()]
    if args.start_index is None or args.end_index is None:
        raise SystemExit("pass --indices or both --start-index and --end-index")
    if args.end_index < args.start_index:
        raise SystemExit("--end-index must be >= --start-index")
    return [str(index) for index in range(args.start_index, args.end_index + 1)]


def _has_option(arguments: list[str], option: str) -> bool:
    return any(argument == option or argument.startswith(option + "=") for argument in arguments)


def _option_value(arguments: list[str], option: str, default: str) -> str:
    for index, argument in enumerate(arguments):
        if argument.startswith(option + "="):
            return argument.split("=", 1)[1]
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1]
    return default


def _option_values(arguments: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument.startswith(option + "="):
            values.append(argument.split("=", 1)[1])
        elif argument == option:
            values.append(arguments[index + 1] if index + 1 < len(arguments) else "")
    return values


def _uses_kimi_direct_api(arguments: list[str]) -> bool:
    env_file = _option_value(arguments, "--remote-api-env-file", "").strip()
    if not env_file:
        return False
    expected = {
        "--llm-model": "kimi-for-coding",
        "--llm-provider": "openai",
        "--remote-proxy-base-url": KIMI_CODING_BASE_URL,
    }
    mismatches = [
        option
        for option, value in expected.items()
        if _option_value(arguments, option, "").rstrip("/") != value
    ]
    if not env_file.startswith("/") or mismatches:
        raise SystemExit(
            "--remote-api-env-file requires an absolute path, --llm-model kimi-for-coding, "
            f"--llm-provider openai, and --remote-proxy-base-url {KIMI_CODING_BASE_URL}"
        )
    return True


def _required_runtime_options(
    *,
    solver_name: str,
    requirements: tuple[tuple[str, str], ...],
    arguments: list[str],
) -> tuple[list[str], dict[str, str]]:
    delegated: list[str] = []
    values: dict[str, str] = {}
    for option, environment_key in requirements:
        cli_value = _option_value(arguments, option, "").strip()
        if cli_value:
            values[option] = cli_value
            continue
        if _has_option(arguments, option):
            raise SystemExit(f"{solver_name} requires a non-empty {option}")
        environment_value = os.environ.get(environment_key, "").strip()
        if not environment_value:
            raise SystemExit(
                f"{solver_name} requires {option} or {environment_key}"
            )
        delegated += [option, environment_value]
        values[option] = environment_value
    return delegated, values


def _require_claude_code_configuration(
    *,
    remaining: list[str],
    runtime_values: dict[str, str],
    fixed_args: dict[str, object],
    max_task_starts: int,
) -> None:
    expected_runtime = {
        "--model-name": "claude-code-2.1.175-glm-5.2",
        "--llm-model": "glm-5.2",
    }
    for option, expected in expected_runtime.items():
        values = _option_values(remaining, option)
        if len(values) > 1 or runtime_values.get(option) != expected:
            raise SystemExit(f"claude-code requires {option} {expected}")
    fixed_options = {
        "--max-task-starts": str(max_task_starts),
        "--context-window": "200000",
        "--llm-provider": "anthropic",
        **{
            "--" + key.replace("_", "-"): str(value)
            for key, value in fixed_args.items()
        },
    }
    for option, expected in fixed_options.items():
        values = _option_values(remaining, option)
        if not values:
            continue
        if len(values) > 1 or option == "--openhands-command" or values[0] != expected:
            raise SystemExit(f"claude-code requires its fixed {option} configuration")


def _without_launch_options(arguments: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in {"--detach", "--no-persistent-proxy"}:
            continue
        if argument == "--launchd-label":
            skip_next = True
            continue
        if argument.startswith("--launchd-label="):
            continue
        cleaned.append(argument)
    return cleaned


def _safe_label(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]+", ".", value.strip()).strip(".")
    return normalized or "run"


def _absolute_pythonpath(value: str) -> str:
    entries = []
    for item in value.split(os.pathsep):
        path = Path(item).expanduser() if item else Path.cwd()
        entries.append(str(path if path.is_absolute() else Path(os.path.abspath(path))))
    return os.pathsep.join(entries)


def _launchd_plist(
    *,
    label: str,
    program_arguments: list[str],
    stdout_path: Path,
    stderr_path: Path,
    keep_alive: bool = False,
) -> dict:
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONUNBUFFERED": "1",
    }
    if pythonpath := os.environ.get("PYTHONPATH", "").strip():
        environment["PYTHONPATH"] = _absolute_pythonpath(pythonpath)
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(WORKSPACE_ROOT),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
    }


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)


def _launchctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def _remote_proxy_healthy(
    *, ssh_command: str, host: str, base_url: str, upstream_base_url: str
) -> bool:
    expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
    probe = (
        "import json,sys,urllib.request;"
        "v=json.load(urllib.request.urlopen(sys.argv[1],timeout=5));"
        "raise SystemExit(0 if v.get('kind')=='authenticated_model_relay' "
        "and v.get('upstream_base_url_sha256')==sys.argv[2] else 3)"
    )
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        shlex.join(
            ["python3", "-c", probe, base_url.rstrip("/") + "/healthz", expected]
        ),
    ]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _remote_proxy_socket_healthy(
    *, ssh_command: str, host: str, socket_path: str, upstream_base_url: str
) -> bool:
    expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
    probe = "\n".join(
        (
            "import http.client,json,os,socket,stat,sys",
            "path,expected=sys.argv[1:3]",
            "mode=os.stat(path).st_mode",
            "assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) & 0o077 == 0",
            "client=socket.socket(socket.AF_UNIX)",
            "client.settimeout(5)",
            "client.connect(path)",
            'client.sendall(b"GET /healthz HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n")',
            "stream=client.makefile('rb')",
            "status=stream.readline().split()",
            "assert len(status) >= 2 and status[1] == b'200'",
            "headers=http.client.parse_headers(stream)",
            "length=int(headers['Content-Length'])",
            "assert 0 <= length <= 65536",
            "body=stream.read(length)",
            "assert len(body) == length",
            "value=json.loads(body)",
            "assert value.get('kind') == 'authenticated_model_relay'",
            "assert value.get('upstream_base_url_sha256') == expected",
        )
    )
    command = [
        *shlex.split(ssh_command),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        shlex.join(["python3", "-c", probe, socket_path, expected]),
    ]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _remove_stale_remote_proxy_socket(
    *, ssh_command: str, host: str, socket_path: str
) -> None:
    remove_stale_remote_socket(
        ssh_command=ssh_command,
        host=host,
        socket_path=socket_path,
    )


def _local_relay_healthy(base_url: str, upstream_base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=5) as response:
            payload = json.load(response)
        expected = hashlib.sha256(upstream_base_url.rstrip("/").encode()).hexdigest()
        return (
            payload.get("kind") == "authenticated_model_relay"
            and payload.get("upstream_base_url_sha256") == expected
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _ensure_local_proxy_agent(
    *, output_dir: Path, remaining: list[str], upstream_base_url: str
) -> dict:
    local_url = _option_value(remaining, "--local-proxy-base-url", "http://127.0.0.1:8878")
    parsed = urllib.parse.urlparse(local_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"}:
        raise RuntimeError("local proxy URL must be a loopback origin")
    port = int(parsed.port or 8878)
    env_file = _option_value(remaining, "--proxy-env-file", "").strip()
    if not env_file or not upstream_base_url.strip():
        raise RuntimeError("starting the local model relay requires --proxy-env-file and --proxy-upstream-base-url")
    if _local_relay_healthy(local_url, upstream_base_url):
        return {"status": "already_healthy", "local_url": local_url}
    label = f"com.opencollab.llmproxy.{port}"
    target = f"gui/{os.getuid()}/{label}"
    plist_path = output_dir / f"{label}.plist"
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    payload = _launchd_plist(
        label=label,
        program_arguments=[
            sys.executable,
            "-m",
            "opencollab_eval.commands.llm_api_proxy",
            "--env-file",
            env_file,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--upstream-base-url",
            upstream_base_url,
        ],
        stdout_path=output_dir / "llm-proxy.launch.stdout.log",
        stderr_path=output_dir / "llm-proxy.launch.stderr.log",
        keep_alive=True,
    )
    payload.pop("WorkingDirectory", None)
    payload["EnvironmentVariables"]["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    _write_plist(plist_path, payload)
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    if _launchctl("print", target).returncode == 0:
        _launchctl("bootout", target, check=True)
    shutil.copy2(plist_path, installed_path)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(installed_path), check=True)
    for _ in range(20):
        if _local_relay_healthy(local_url, upstream_base_url):
            return {
                "status": "started",
                "local_url": local_url,
                "upstream_base_url_sha256": hashlib.sha256(upstream_base_url.encode()).hexdigest(),
            }
        time.sleep(0.25)
    raise RuntimeError(f"local model relay failed health check: {local_url}")


def _ensure_proxy_agent(
    *, output_dir: Path, remaining: list[str], upstream_base_url: str = ""
) -> dict:
    host = _option_value(
        remaining,
        "--host",
        os.environ.get("OPENCOLLAB_SWE_HOST", ""),
    ).strip()
    if not host:
        raise RuntimeError("persistent proxy requires --host or OPENCOLLAB_SWE_HOST")
    ssh_command = _option_value(remaining, "--ssh-command", "/usr/bin/ssh")
    local_url = _option_value(remaining, "--local-proxy-base-url", "http://127.0.0.1:8878")
    remote_url = _option_value(remaining, "--remote-proxy-base-url", "http://127.0.0.1:18788")
    local = urllib.parse.urlparse(local_url)
    remote = urllib.parse.urlparse(remote_url)
    if local.hostname not in {"127.0.0.1", "localhost"} or remote.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError("persistent proxy URLs must use localhost")
    local_port = int(local.port or 8878)
    remote_port = int(remote.port or 18788)
    remote_socket = relay_socket_path(remote_url)
    local_relay = _ensure_local_proxy_agent(
        output_dir=output_dir,
        remaining=remaining,
        upstream_base_url=upstream_base_url,
    )
    label = f"com.opencollab.proxy.{_safe_label(host)}.{remote_port}"
    target = f"gui/{os.getuid()}/{label}"
    if _remote_proxy_healthy(
        ssh_command=ssh_command,
        host=host,
        base_url=remote_url,
        upstream_base_url=upstream_base_url,
    ) and _remote_proxy_socket_healthy(
        ssh_command=ssh_command,
        host=host,
        socket_path=remote_socket,
        upstream_base_url=upstream_base_url,
    ):
        return {
            "status": "already_healthy",
            "label": label,
            "remote_url": remote_url,
            "local_relay": local_relay,
        }

    plist_path = output_dir / f"{label}.plist"
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    payload = _launchd_plist(
        label=label,
        program_arguments=[
            sys.executable,
            "-m",
            "opencollab_eval.commands.ssh_reverse_proxy",
            "--ssh-command",
            ssh_command,
            "--host",
            host,
            "--local-port",
            str(local_port),
            "--remote-port",
            str(remote_port),
            "--remote-socket",
            remote_socket,
        ],
        stdout_path=output_dir / "proxy.launch.stdout.log",
        stderr_path=output_dir / "proxy.launch.stderr.log",
        keep_alive=True,
    )
    payload.pop("WorkingDirectory", None)
    _write_plist(plist_path, payload)
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    if _launchctl("print", target).returncode == 0:
        _launchctl("bootout", target, check=True)
    _remove_stale_remote_proxy_socket(
        ssh_command=ssh_command,
        host=host,
        socket_path=remote_socket,
    )
    shutil.copy2(plist_path, installed_path)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(installed_path), check=True)
    for _ in range(12):
        if _remote_proxy_healthy(
            ssh_command=ssh_command,
            host=host,
            base_url=remote_url,
            upstream_base_url=upstream_base_url,
        ) and _remote_proxy_socket_healthy(
            ssh_command=ssh_command,
            host=host,
            socket_path=remote_socket,
            upstream_base_url=upstream_base_url,
        ):
            return {
                "status": "started",
                "label": label,
                "remote_url": remote_url,
                "local_relay": local_relay,
            }
        time.sleep(0.5)
    _launchctl("bootout", target)
    raise RuntimeError(f"persistent proxy failed health check: {remote_url}")


def _launch_detached(args: argparse.Namespace, raw_arguments: list[str], remaining: list[str]) -> int:
    run_id = args.run_id or (
        f"swe_{_safe_label(args.solver)}_{_safe_label(','.join(_normalize_indices(args)))}_"
        + time.strftime("%Y%m%d_%H%M%S")
    )
    output_dir = (
        args.output_dir or WORKSPACE_ROOT / "docs" / "monitoring" / run_id
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    child_arguments = _without_launch_options(raw_arguments)
    if not args.run_id:
        child_arguments += ["--run-id", run_id]
    if args.output_dir is None:
        child_arguments += ["--output-dir", str(output_dir)]
    direct_remote_api = _uses_kimi_direct_api(remaining)
    proxy = (
        {"status": "disabled"}
        if args.no_persistent_proxy or direct_remote_api
        else _ensure_proxy_agent(
            output_dir=output_dir,
            remaining=remaining,
            upstream_base_url=args.proxy_upstream_base_url,
        )
    )
    label = args.launchd_label or f"com.opencollab.eval.{_safe_label(run_id)}"
    target = f"gui/{os.getuid()}/{label}"
    existing = _launchctl("print", target)
    if existing.returncode == 0 and "state = running" in existing.stdout:
        print(
            json.dumps(
                {
                    "status": "already_running",
                    "run_id": run_id,
                    "label": label,
                    "output_dir": str(output_dir),
                    "proxy": proxy,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if existing.returncode == 0:
        _launchctl("bootout", target, check=True)

    plist_path = output_dir / f"{label}.plist"
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    payload = _launchd_plist(
        label=label,
        program_arguments=[
            sys.executable,
            "-m",
            "opencollab_eval.commands.swe_eval_run",
            *child_arguments,
        ],
        stdout_path=output_dir / "runner.launch.stdout.log",
        stderr_path=output_dir / "runner.launch.stderr.log",
    )
    _write_plist(plist_path, payload)
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plist_path, installed_path)
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(installed_path), check=True)
    status = {
        "status": "started",
        "run_id": run_id,
        "label": label,
        "output_dir": str(output_dir),
        "plist": str(plist_path),
        "proxy": proxy,
    }
    (output_dir / "launch_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def _reject_historical_eval_options(remaining: list[str]) -> None:
    historical_eval_options = [
        option
        for option in ("--eval-only", "--parent-output-dir")
        if _has_option(remaining, option)
    ]
    if historical_eval_options:
        raise SystemExit(
            "historical eval-only is a single-task maintenance API; "
            "use python -m opencollab_eval.commands.swe_v1_prolite_runner directly"
        )


def _run_parallel_runner(args: argparse.Namespace, remaining: list[str]) -> int:
    spec = workflow_solver_spec(args.solver)
    _reject_historical_eval_options(remaining)
    if _has_option(remaining, "--workflow"):
        raise SystemExit("--workflow is selected by --solver and cannot be overridden")
    runner_name = "swe_g11_parallel_runner"
    runner = _load_module(runner_name)
    delegated = [f"opencollab_eval.commands.{runner_name}"]
    if args.indices:
        delegated += ["--indices", args.indices]
    else:
        delegated += ["--start-index", str(args.start_index), "--end-index", str(args.end_index)]
    workers = 5 if args.workers is None else args.workers
    if spec.name == "claude-code":
        workers = 1 if args.workers is None else args.workers
        if not 1 <= workers <= 4:
            raise SystemExit("claude-code requires --workers between 1 and 4")
    delegated += ["--max-workers", str(workers), "--workflow", spec.workflow_name]
    if not _has_option(remaining, "--max-task-starts"):
        delegated += ["--max-task-starts", str(spec.max_attempts)]
    if spec.default_budget_tokens is not None and not _has_option(remaining, "--budget"):
        delegated += ["--budget", str(spec.default_budget_tokens)]
    required_arguments, runtime_values = _required_runtime_options(
        solver_name=spec.name,
        requirements=spec.required_runtime_options,
        arguments=remaining,
    )
    if spec.name == "claude-code":
        _require_claude_code_configuration(
            remaining=remaining,
            runtime_values=runtime_values,
            fixed_args=spec.args,
            max_task_starts=spec.max_attempts,
        )
    delegated += required_arguments
    model = runtime_values.get("--llm-model", "")
    configured_context = spec.config_overrides.get("context_window")
    if configured_context is not None and _has_option(remaining, "--context-window"):
        requested_context = _option_value(remaining, "--context-window", "")
        if requested_context != str(configured_context):
            raise SystemExit(
                f"{spec.name} requires --context-window {configured_context}"
            )
    if not _has_option(remaining, "--context-window"):
        context_window = configured_context or model_context_window(model)
        if context_window is not None:
            delegated += ["--context-window", str(context_window)]
    for key, option in (
        ("temperature", "--temperature"),
        ("top_p", "--top-p"),
        ("max_output_tokens", "--max-output-tokens"),
    ):
        value = spec.config_overrides.get(key)
        if value is not None and not _has_option(remaining, option):
            delegated += [option, str(value)]
    for key, value in spec.args.items():
        option = "--" + key.replace("_", "-")
        if not _has_option(remaining, option):
            delegated += [option, str(value)]
    if args.run_id:
        delegated += ["--run-id", args.run_id]
    if args.output_dir:
        delegated += ["--output-dir", str(args.output_dir)]
    delegated += remaining
    sys.argv = delegated
    return int(runner.main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SWE evaluation with a selected solver.")
    parser.add_argument("--dataset", default="swe-batch-pro-lite")
    parser.add_argument("--indices")
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--solver", default="g11", choices=tuple(DEFAULT_WORKFLOW_SOLVERS))
    parser.add_argument("--workers", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--launchd-label")
    parser.add_argument("--no-persistent-proxy", action="store_true")
    parser.add_argument(
        "--proxy-upstream-base-url",
        default=os.environ.get("OPENCOLLAB_PROXY_UPSTREAM_BASE_URL", ""),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    args, remaining = parser.parse_known_args(raw_arguments)
    if args.dataset != "swe-batch-pro-lite":
        raise SystemExit(f"unsupported dataset: {args.dataset}")
    _normalize_indices(args)
    _reject_historical_eval_options(remaining)
    if args.detach:
        return _launch_detached(args, raw_arguments, remaining)
    direct_remote_api = _uses_kimi_direct_api(remaining)
    if not args.no_persistent_proxy and not direct_remote_api and not _has_option(remaining, "--dry-run"):
        run_id = args.run_id or (
            f"swe_{_safe_label(args.solver)}_{_safe_label(','.join(_normalize_indices(args)))}_"
            + time.strftime("%Y%m%d_%H%M%S")
        )
        args.run_id = run_id
        args.output_dir = (
            args.output_dir or WORKSPACE_ROOT / "docs" / "monitoring" / run_id
        ).resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _ensure_proxy_agent(
            output_dir=args.output_dir,
            remaining=remaining,
            upstream_base_url=args.proxy_upstream_base_url,
        )
    return _run_parallel_runner(args, remaining)


if __name__ == "__main__":
    raise SystemExit(main())
