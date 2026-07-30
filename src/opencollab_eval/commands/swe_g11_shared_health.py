"""Direct probes for services shared by a remote SWE evaluation batch."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
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
_TRANSIENT_MODEL_PROBE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_MODEL_PROBE_DELAY_SECONDS = 60.0


def response_model_matches(provider: str, requested: str, actual: object) -> bool:
    """Require the selected model identity, retaining documented Kimi aliases."""
    if provider == "openai" and is_kimi_direct_model(requested):
        return kimi_response_model_matches(requested, actual)
    requested_model = requested.strip().lower()
    actual_model = str(actual or "").strip().lower()
    return bool(requested_model) and actual_model == requested_model


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
    wire_protocol = workflow_env.get(
        "OPENCOLLAB_WIRE_PROTOCOL", "chat_completions"
    ).strip().lower()
    if wire_protocol not in {"chat_completions", "responses"}:
        raise ValueError(f"unsupported wire protocol: {wire_protocol}")
    if config.llm_provider != "openai" and wire_protocol != "chat_completions":
        raise ValueError(
            f"{config.llm_provider} does not support wire protocol {wire_protocol}"
        )
    legacy_thinking = workflow_env.get("OPENCOLLAB_THINKING", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    thinking_params = (
        json.loads(workflow_env.get("OPENCOLLAB_THINKING_PARAMS", "{}"))
        if legacy_thinking
        else {}
    )
    reasoning_effort = (
        workflow_env.get("OPENCOLLAB_REASONING_EFFORT", "").strip() or None
    )
    if (
        reasoning_effort is None
        and wire_protocol == "responses"
        and isinstance(thinking_params.get("reasoning_effort"), str)
    ):
        reasoning_effort = thinking_params["reasoning_effort"]
    thinking = legacy_thinking or reasoning_effort is not None
    options = {
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_output_tokens,
        **thinking_params,
    }
    if wire_protocol == "responses":
        options["reasoning_effort"] = reasoning_effort
    model_name = str(getattr(config, "model_name", "") or "")
    claude_version = re.fullmatch(r"claude-code-(\d+\.\d+\.\d+)-.+", model_name)
    probe_user_agent = (
        f"claude-cli/{claude_version.group(1)}"
        if config.llm_provider == "anthropic" and claude_version
        else "Anthropic/Python opencollab-eval"
        if config.llm_provider == "anthropic"
        else ""
    )
    script = r'''import datetime,email.utils,json,math,os,sys,urllib.error,urllib.request
from opencollab_eval.engine.swe_v1_remote_state import bind_remote_api_network_environment,read_remote_api_environment
base,provider,model,wire,thinking_text,options_text,env_path,user_agent=sys.argv[1:9]
thinking=thinking_text == "true"
options=json.loads(options_text)
remote=read_remote_api_environment(env_path) if env_path else {"token":sys.stdin.readline().strip(),"network_env":{}}
token=remote["token"]
bind_remote_api_network_environment(os.environ,remote["network_env"])
if provider == "anthropic":
    path="/v1/messages"
    payload={"model":model,"max_tokens":128,"messages":[{"role":"user","content":"Reply OK"}]}
    headers={"x-api-key":token,"anthropic-version":"2023-06-01","content-type":"application/json"}
elif wire == "responses":
    path="/responses"
    payload={
        "model":model,
        "input":[{"role":"user","content":[{"type":"input_text","text":"Reply OK"}]}],
        "store":False,
    }
    for key in ("temperature","top_p"):
        if options.get(key) is not None:
            payload[key]=options[key]
    if options.get("max_tokens") is not None:
        payload["max_output_tokens"]=options["max_tokens"]
    reasoning_effort=options.get("reasoning_effort")
    if reasoning_effort is not None:
        payload["reasoning"]={"effort":reasoning_effort}
    headers={"Authorization":"Bearer "+token,"content-type":"application/json"}
else:
    path="/chat/completions"
    payload={"model":model,"messages":[{"role":"user","content":"Reply OK"}]}
    payload.update({key:value for key,value in options.items() if value is not None})
    headers={"Authorization":"Bearer "+token,"content-type":"application/json"}
if user_agent:
    headers["User-Agent"]=user_agent
request=urllib.request.Request(base.rstrip("/")+path,data=json.dumps(payload).encode(),headers=headers,method="POST")
try:
    with urllib.request.urlopen(request,timeout=120) as response:
        value=json.load(response)
except urllib.error.HTTPError as exc:
    retry_after_seconds=None
    retry_after=exc.headers.get("Retry-After")
    if retry_after is not None:
        try:
            retry_after_seconds=float(retry_after)
        except (TypeError,ValueError):
            try:
                retry_at=email.utils.parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at=retry_at.replace(tzinfo=datetime.timezone.utc)
                retry_after_seconds=(retry_at-datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            except (TypeError,ValueError,OverflowError):
                retry_after_seconds=None
        if retry_after_seconds is not None:
            valid_retry_after=math.isfinite(retry_after_seconds) and retry_after_seconds >= 0
            retry_after_seconds=min(retry_after_seconds,300.0) if valid_retry_after else None
    try:
        error_payload=json.loads(exc.read(4097)[:4096])
        candidate=error_payload.get("error",{}).get("type", "")
        error_type=candidate if candidate in {"access_terminated_error"} else ""
    except Exception:
        error_type=""
    print(json.dumps({"status":"http_error","code":exc.code,"error_type":error_type,"retry_after_seconds":retry_after_seconds}))
    raise SystemExit(3)
except Exception:
    print(json.dumps({"status":"transport_error"}))
    raise SystemExit(3)
if provider == "anthropic":
    valid=bool(value.get("content"))
    thinking_proven=not thinking or any(item.get("type") == "thinking" for item in value.get("content",[]))
    thinking_request_bound=thinking_proven
elif wire == "responses":
    output=value.get("output",[])
    valid=value.get("status") == "completed" and isinstance(output,list) and bool(output)
    reasoning_effort=options.get("reasoning_effort")
    thinking_proven=not thinking or any(
        isinstance(item,dict) and item.get("type") == "reasoning" for item in output
    )
    thinking_request_bound=not thinking or isinstance(reasoning_effort,str)
    thinking_evidence=(
        "not_requested" if not thinking else
        "response_reasoning_item" if thinking_proven else
        "requested_reasoning_effort" if thinking_request_bound else
        "missing"
    )
else:
    valid=isinstance(value.get("choices"),list) and bool(value["choices"])
    message=value["choices"][0].get("message",{}) if valid else {}
    if not thinking:
        thinking_evidence="not_requested"
        thinking_request_bound=True
        thinking_proven=True
    elif message.get("reasoning_content"):
        thinking_evidence="response_reasoning_content"
        thinking_request_bound=True
        thinking_proven=True
    elif isinstance(options.get("reasoning_effort"),str):
        thinking_evidence="requested_reasoning_effort"
        thinking_request_bound=True
        thinking_proven=False
    elif options:
        thinking_evidence="requested_thinking_config"
        thinking_request_bound=True
        thinking_proven=False
    else:
        thinking_evidence="missing"
        thinking_request_bound=False
        thinking_proven=False
actual_model=value.get("model")
valid=valid and (thinking_proven if provider == "anthropic" else thinking_request_bound)
probe_status="ok" if valid else "invalid_response"
if wire == "responses" and value.get("status") == "completed" and value.get("output") == []:
    probe_status="empty_output"
print(json.dumps({
    "status":probe_status,
    "thinking_proven":thinking_proven,
    "thinking_request_bound":thinking_request_bound,
    "thinking_evidence":thinking_evidence if provider != "anthropic" else (
        "response_thinking_block" if thinking else "not_requested"
    ),
    "actual_model":actual_model,
    "wire_protocol":wire,
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
                wire_protocol,
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
    response_wire_protocol = payload.get("wire_protocol", wire_protocol)
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or error_type not in _REMOTE_ERROR_TYPES:
        error_type = None
    model_matches = response_model_matches(
        config.llm_provider,
        config.llm_model,
        actual_model,
    )
    summary = {
        "status": payload.get("status"),
        "direct": True,
        "scope": "shared_infrastructure",
        "http_status": payload.get("code"),
        "retry_after_seconds": payload.get("retry_after_seconds"),
        "remote_error_type": error_type,
        "provider": config.llm_provider,
        "model": config.llm_model,
        "response_model": actual_model,
        "model_matches": model_matches,
        "wire_protocol": wire_protocol,
        "response_wire_protocol": response_wire_protocol,
        "wire_protocol_matches": response_wire_protocol == wire_protocol,
        "thinking_enabled": thinking,
        "thinking_proven": payload.get("thinking_proven") is True,
        "thinking_request_bound": payload.get("thinking_request_bound") is True,
        "thinking_evidence": payload.get("thinking_evidence"),
        "base_url_sha256": hashlib.sha256(config.remote_proxy_base_url.encode()).hexdigest(),
    }
    if (
        result.returncode != 0
        or summary["status"] != "ok"
        or not summary["model_matches"]
        or not summary["wire_protocol_matches"]
    ):
        raise SharedProbeFailure(
            f"remote model probe failed rc={result.returncode} status={summary['status']}",
            summary,
        )
    return summary


def _model_probe_failure_is_transient(result: dict[str, Any]) -> bool:
    status = result.get("http_status")
    empty_output = (
        result.get("status") == "empty_output"
        and result.get("model_matches") is True
        and result.get("wire_protocol_matches") is True
        and (
            result.get("thinking_enabled") is not True
            or result.get("thinking_request_bound") is True
        )
    )
    return (
        status in _TRANSIENT_MODEL_PROBE_HTTP_STATUSES
        or result.get("failure_kind") == "timeout"
        or empty_output
        or result.get("status") == "transport_error"
    )


def _interruptible_wait(delay: float, interrupted: Callable[[], bool]) -> None:
    deadline = time.monotonic() + max(0.0, delay)
    while True:
        if interrupted():
            raise InterruptedError("parallel evaluation interrupted")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def wait_for_remote_model_probe(
    config: Any,
    *,
    run_probe: Callable[[Any], dict[str, Any]],
    write_json: Callable[[Path, object], None],
    interrupted: Callable[[], bool],
) -> dict[str, Any]:
    """Wait through transient provider outages before starting any task."""
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    ledger_path = config.output_dir / "model_probe_attempts.json"
    while True:
        if interrupted():
            raise InterruptedError("parallel evaluation interrupted")
        try:
            result = run_probe(config)
        except SharedProbeFailure as exc:
            elapsed = max(0.0, time.monotonic() - started)
            attempts.append(
                {
                    "attempt": len(attempts) + 1,
                    "elapsed_seconds": round(elapsed, 3),
                    "result": exc.result,
                }
            )
            if not _model_probe_failure_is_transient(exc.result):
                write_json(ledger_path, {"status": "failed", "attempts": attempts})
                raise
            deadline = started + max(0, getattr(config, "total_timeout", 0))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exhausted = {
                    **exc.result,
                    "status": "failed",
                    "failure_kind": "retry_deadline_exhausted",
                    "probe_attempts": len(attempts),
                }
                write_json(
                    ledger_path,
                    {"status": "retry_deadline_exhausted", "attempts": attempts},
                )
                raise SharedProbeFailure(
                    "remote model probe retry deadline exhausted",
                    exhausted,
                ) from exc
            retry_after = exc.result.get("retry_after_seconds")
            if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
                retry_after = getattr(config, "retry_delay_seconds", 60) * (
                    2 ** min(len(attempts) - 1, 6)
                )
            delay = min(
                max(0.0, float(retry_after)),
                _MAX_MODEL_PROBE_DELAY_SECONDS,
                remaining,
            )
            write_json(
                ledger_path,
                {
                    "status": "waiting",
                    "next_delay_seconds": round(delay, 3),
                    "attempts": attempts,
                },
            )
            _interruptible_wait(delay, interrupted)
            continue
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
                "result": result,
            }
        )
        write_json(ledger_path, {"status": "ok", "attempts": attempts})
        return result


__all__ = [
    "SharedProbeFailure",
    "remote_health_script",
    "response_model_matches",
    "run_remote_health_checks",
    "run_remote_model_probe",
    "wait_for_remote_model_probe",
]
