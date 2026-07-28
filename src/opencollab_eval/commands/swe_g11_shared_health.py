"""Direct probes for services shared by a remote SWE evaluation batch."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from opencollab_eval.commands.swe_ssh_transport import (
    CheckedCommandError,
    run_ssh_checked,
)
from opencollab_eval.commands.swe_v1_prolite_config import get_proxy_token
from opencollab_eval.engine.solver_backend import (
    is_kimi_direct_model,
    kimi_response_model_matches,
)

_REMOTE_ERROR_TYPES = frozenset({"access_terminated_error"})


class SharedProbeFailure(RuntimeError):
    """A shared-service request was issued and returned failed evidence."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result
        self.direct_probe_completed = True


def remote_health_script(config: Any) -> str:
    remote_base = shlex.quote(config.remote_base)
    remote_runtime_repo = shlex.quote(config.remote_runtime_repo)
    remote_python = shlex.quote(config.remote_python)
    return "\n".join(
        [
            "set -eu",
            f"mkdir -p {remote_base}",
            f"test -x {remote_python} || command -v {remote_python} >/dev/null",
            "command -v docker >/dev/null",
            "docker info >/dev/null",
            f"df -Pk {remote_base}",
            f"test -d {remote_runtime_repo}",
            f"probe=$(mktemp {remote_base}/.opencollab-health.XXXXXX)",
            "trap 'rm -f \"$probe\"' EXIT HUP INT TERM",
            "printf 'opencollab-health' > \"$probe\"",
            "test \"$(cat \"$probe\")\" = opencollab-health",
            "rm -f \"$probe\"",
            "trap - EXIT HUP INT TERM",
        ]
    )


def run_remote_health_checks(
    config: Any,
    *,
    repo: Path,
    write_json,
    write_text,
) -> dict[str, Any]:
    json_path = config.output_dir / "remote_health_check.json"
    stdout_path = config.output_dir / "remote_health_check.stdout.log"
    stderr_path = config.output_dir / "remote_health_check.stderr.log"
    if config.skip_health_checks or config.dry_run:
        result = {
            "status": "skipped",
            "reason": "disabled" if config.skip_health_checks else "dry_run",
        }
        write_json(json_path, result)
        return result
    command = [
        *shlex.split(config.ssh_command),
        config.host,
        "bash -lc " + shlex.quote(remote_health_script(config)),
    ]
    attempts: list[dict[str, object]] = []
    try:
        proc = run_ssh_checked(
            command,
            timeout=120,
            cwd=repo,
            retry_log=attempts,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "status": "failed",
            "direct": True,
            "scope": "shared_infrastructure",
            "failure_kind": "timeout",
            "attempts": attempts,
        }
        write_json(json_path, result)
        raise SharedProbeFailure("remote health check timed out", result) from exc
    except CheckedCommandError as exc:
        write_text(stdout_path, exc.stdout)
        write_text(stderr_path, exc.stderr)
        result = {
            "status": "failed",
            "direct": True,
            "scope": "shared_infrastructure",
            "returncode": exc.returncode,
            "attempts": attempts,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        }
        write_json(json_path, result)
        raise SharedProbeFailure(
            f"remote health check failed rc={exc.returncode}",
            result,
        ) from exc
    write_text(stdout_path, proc.stdout)
    write_text(stderr_path, proc.stderr)
    result = {
        "status": "ok",
        "direct": True,
        "scope": "shared_infrastructure",
        "returncode": proc.returncode,
        "attempts": attempts,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    write_json(json_path, result)
    return result


def run_remote_model_probe(config: Any, *, get_token=get_proxy_token) -> dict[str, Any]:
    """Prove that the selected remote model endpoint completes one request."""
    if config.skip_health_checks or config.dry_run:
        return {
            "status": "skipped",
            "reason": "disabled" if config.skip_health_checks else "dry_run",
        }
    workflow_env = dict(item.split("=", 1) for item in config.workflow_env)
    thinking = workflow_env.get("OPENCOLLAB_THINKING", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    thinking_params = workflow_env.get("OPENCOLLAB_THINKING_PARAMS", "{}") if thinking else "{}"
    options = {
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_output_tokens,
        **json.loads(thinking_params),
    }
    model_name = str(getattr(config, "model_name", "") or "")
    claude_version = re.fullmatch(r"claude-code-(\d+\.\d+\.\d+)-.+", model_name)
    probe_user_agent = (
        f"claude-cli/{claude_version.group(1)}"
        if config.llm_provider == "anthropic" and claude_version
        else ""
    )
    script = r'''import json,os,sys,urllib.error,urllib.request
from opencollab_eval.engine.swe_v1_remote_state import bind_remote_api_network_environment,read_remote_api_environment
base,provider,model,thinking_text,options_text,env_path,user_agent=sys.argv[1:8]
thinking=thinking_text == "true"
remote=read_remote_api_environment(env_path) if env_path else {"token":sys.stdin.readline().strip(),"network_env":{}}
token=remote["token"]
bind_remote_api_network_environment(os.environ,remote["network_env"])
if provider == "anthropic":
    path="/v1/messages"
    payload={"model":model,"max_tokens":128,"messages":[{"role":"user","content":"Reply OK"}]}
    headers={"x-api-key":token,"anthropic-version":"2023-06-01","content-type":"application/json"}
else:
    path="/chat/completions"
    payload={"model":model,"messages":[{"role":"user","content":"Reply OK"}]}
    payload.update({key:value for key,value in json.loads(options_text).items() if value is not None})
    headers={"Authorization":"Bearer "+token,"content-type":"application/json"}
if user_agent:
    headers["User-Agent"]=user_agent
request=urllib.request.Request(base.rstrip("/")+path,data=json.dumps(payload).encode(),headers=headers,method="POST")
try:
    with urllib.request.urlopen(request,timeout=120) as response:
        value=json.load(response)
except urllib.error.HTTPError as exc:
    try:
        error_payload=json.loads(exc.read(4097)[:4096])
        candidate=error_payload.get("error",{}).get("type", "")
        error_type=candidate if candidate in {"access_terminated_error"} else ""
    except Exception:
        error_type=""
    print(json.dumps({"status":"http_error","code":exc.code,"error_type":error_type}))
    raise SystemExit(3)
except Exception:
    print(json.dumps({"status":"transport_error"}))
    raise SystemExit(3)
if provider == "anthropic":
    valid=bool(value.get("content"))
    thinking_proven=not thinking or any(item.get("type") == "thinking" for item in value.get("content",[]))
else:
    valid=isinstance(value.get("choices"),list) and bool(value["choices"])
    message=value["choices"][0].get("message",{}) if valid else {}
    thinking_proven=not thinking or bool(message.get("reasoning_content"))
actual_model=value.get("model")
valid=valid and thinking_proven
print(json.dumps({
    "status":"ok" if valid else "invalid_response",
    "thinking_proven":thinking_proven,
    "actual_model":actual_model,
}))
raise SystemExit(0 if valid else 3)
'''
    command = [
        *shlex.split(config.ssh_command),
        "-o",
        "BatchMode=yes",
        config.host,
        "env PYTHONPATH="
        + shlex.quote(str(Path(config.remote_runtime_repo) / "src"))
        + " "
        + shlex.quote(config.remote_python)
        + " -c "
        + shlex.quote(script)
        + " "
        + " ".join(
            shlex.quote(value)
            for value in (
                config.remote_proxy_base_url,
                config.llm_provider,
                config.llm_model,
                "true" if thinking else "false",
                json.dumps(options, separators=(",", ":")),
                config.remote_api_env_file,
                probe_user_agent,
            )
        ),
    ]
    probe_input = "" if config.remote_api_env_file else get_token(config.proxy_env_file) + "\n"
    try:
        result = subprocess.run(
            command,
            input=probe_input,
            text=True,
            capture_output=True,
            timeout=150,
        )
    except subprocess.TimeoutExpired as exc:
        summary = {
            "status": "failed",
            "direct": True,
            "scope": "shared_infrastructure",
            "failure_kind": "timeout",
            "provider": config.llm_provider,
            "model": config.llm_model,
        }
        raise SharedProbeFailure("remote model probe timed out", summary) from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"status": "invalid_probe_output"}
    actual_model = payload.get("actual_model")
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or error_type not in _REMOTE_ERROR_TYPES:
        error_type = None
    model_matches = (
        kimi_response_model_matches(config.llm_model, actual_model)
        if config.llm_provider == "openai" and is_kimi_direct_model(config.llm_model)
        else bool(actual_model)
    )
    summary = {
        "status": payload.get("status"),
        "direct": True,
        "scope": "shared_infrastructure",
        "http_status": payload.get("code"),
        "remote_error_type": error_type,
        "provider": config.llm_provider,
        "model": config.llm_model,
        "response_model": actual_model,
        "model_matches": model_matches,
        "thinking_enabled": thinking,
        "thinking_proven": payload.get("thinking_proven") is True,
        "base_url_sha256": hashlib.sha256(config.remote_proxy_base_url.encode()).hexdigest(),
    }
    if result.returncode != 0 or summary["status"] != "ok" or not summary["model_matches"]:
        raise SharedProbeFailure(
            f"remote model probe failed rc={result.returncode} status={summary['status']}",
            summary,
        )
    return summary


__all__ = [
    "SharedProbeFailure",
    "remote_health_script",
    "run_remote_health_checks",
    "run_remote_model_probe",
]
