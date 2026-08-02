"""Build and validate evidence emitted by the Claude Code external solver."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

SCHEMA = "opencollab.external_solver.v1"
SOLVER = "claude-code"
EXPECTED_MODEL = "glm-5.2"
EXPECTED_CLI_VERSION = "2.1.175"
MAX_STREAM_BYTES = 512 * 1024 * 1024
RUNTIME_ENV_KEYS = (
    "OPENCOLLAB_CLAUDE_EXPECTED_MODEL",
    "OPENCOLLAB_CLAUDE_EXPECTED_VERSION",
    "OPENCOLLAB_CLAUDE_RUNTIME_IMAGE",
    "OPENCOLLAB_CLAUDE_RUNTIME_IMAGE_ID",
)


def validate_runtime_workflow_settings(values: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for value in values:
        name, separator, configured = value.partition("=")
        if not separator or name not in RUNTIME_ENV_KEYS:
            continue
        if name in settings:
            raise ValueError(f"claude-code requires exactly one {name} workflow setting")
        settings[name] = configured.strip()
    missing = sorted(set(RUNTIME_ENV_KEYS) - set(settings))
    if missing:
        raise ValueError("claude-code requires workflow settings: " + ", ".join(missing))
    expected = {
        "OPENCOLLAB_CLAUDE_EXPECTED_MODEL": EXPECTED_MODEL,
        "OPENCOLLAB_CLAUDE_EXPECTED_VERSION": EXPECTED_CLI_VERSION,
    }
    for name, value in expected.items():
        if settings[name] != value:
            raise ValueError(f"claude-code requires {name}={value}")
    if not settings["OPENCOLLAB_CLAUDE_RUNTIME_IMAGE"]:
        raise ValueError("claude-code requires a non-empty runtime image")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", settings["OPENCOLLAB_CLAUDE_RUNTIME_IMAGE_ID"]) is None:
        raise ValueError("claude-code requires an immutable runtime image SHA-256")
    return settings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_settings(path: Path, base_url: str) -> None:
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": EXPECTED_MODEL,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": EXPECTED_MODEL,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": EXPECTED_MODEL,
            "API_TIMEOUT_MS": "3000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    }
    path.write_text(
        json.dumps(settings, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def relay_upstream_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Claude relay upstream must be an uncredentialed loopback HTTP URL")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"host.docker.internal{port}", parsed.path, parsed.query, "")
    )


def relay_socket_path(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Claude relay socket requires an uncredentialed loopback HTTP origin")
    return f"/tmp/opencollab-llmproxy-{parsed.port or 80}.sock"


def write_prompt(source: Path, output: Path, workspace: Path, wrapper: Path) -> None:
    prompt = source.read_text(encoding="utf-8").replace("/testbed", str(workspace))
    prompt += (
        "\n\nRun every repository command and test through the wrapper below. Pass "
        "the command as bash -lc followed by one quoted command string.\n"
        f"{wrapper} bash -lc '<command>'\n"
        "The wrapper executes inside the offline task container while file edits "
        "remain visible in this working directory. Do not use network tools or "
        "inspect paths outside this workspace.\n"
    )
    output.write_text(prompt, encoding="utf-8")


def _stream_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > MAX_STREAM_BYTES:
        raise ValueError("Claude stream is missing or exceeds the evidence limit")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Claude stream line {line_number} is not JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Claude stream line {line_number} is not an object")
            rows.append(row)
    return rows


def _nonnegative_int(value: Any, field: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"invalid {field}")
        return 0
    return value


def build_sidecar(
    *,
    stream_path: Path,
    settings_path: Path,
    executable_path: Path | str,
    executable_sha256: str = "",
    cli_version_output: str,
    process_returncode: int,
    runtime_image: str = "",
    runtime_image_id: str = "",
    expected_runtime_image_id: str = "",
    task_image_id: str = "",
    solver_task_id: str = "",
    prompt_sha256: str = "",
    anonymous_head: str = "",
    base_tree: str = "",
    raw_patch_sha256: str = "",
    candidate_tree: str = "",
) -> dict[str, Any]:
    errors: list[str] = []
    version_match = re.search(r"(?<![0-9.])(\d+\.\d+\.\d+)(?![0-9.])", cli_version_output)
    cli_version = version_match.group(1) if version_match else ""
    if cli_version != EXPECTED_CLI_VERSION:
        errors.append(f"unexpected Claude Code version {cli_version or 'unknown'}")
    if process_returncode != 0:
        errors.append(f"Claude Code exited with {process_returncode}")
    if runtime_image_id != expected_runtime_image_id or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", runtime_image_id
    ):
        errors.append("Claude runtime image identity mismatch")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", task_image_id) is None:
        errors.append("task image identity is invalid")
    binding = {
        "solver_task_id": solver_task_id,
        "prompt_sha256": prompt_sha256,
        "anonymous_head": anonymous_head,
        "base_tree": base_tree,
        "raw_patch_sha256": raw_patch_sha256,
        "candidate_tree": candidate_tree,
        "task_image_id": task_image_id,
    }
    hash_valid = {
        "prompt_sha256": re.fullmatch(r"[0-9a-f]{64}", prompt_sha256),
        "raw_patch_sha256": re.fullmatch(r"[0-9a-f]{64}", raw_patch_sha256),
        "candidate_tree": re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate_tree
        ),
        "anonymous_head": re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", anonymous_head),
        "base_tree": re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_tree),
    }
    if not solver_task_id or not all(hash_valid.values()):
        errors.append("Claude invocation binding is incomplete")
    try:
        rows = _stream_rows(stream_path)
    except (OSError, ValueError) as exc:
        rows = []
        errors.append(str(exc))

    init_rows = [row for row in rows if row.get("type") == "system" and row.get("subtype") == "init"]
    result_rows = [row for row in rows if row.get("type") == "result"]
    if len(init_rows) != 1:
        errors.append("Claude stream must contain exactly one init record")
    if len(result_rows) != 1:
        errors.append("Claude stream must contain exactly one result record")
    stream_cli_version = init_rows[0].get("claude_code_version") if len(init_rows) == 1 else None
    if stream_cli_version != EXPECTED_CLI_VERSION:
        errors.append("Claude stream reports an unexpected CLI version")

    observed_models: set[str] = set()
    synthetic_events = 0
    transport_failure = False
    if init_rows and isinstance(init_rows[0].get("model"), str):
        observed_models.add(init_rows[0]["model"])
    for row in rows:
        message = row.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            if message["model"] == "<synthetic>":
                synthetic_events += 1
                content = message.get("content")
                api_error = False
                if isinstance(content, list):
                    texts = [
                        str(item.get("text"))
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    api_error = any(text.startswith("API Error:") for text in texts)
                    transport_failure = transport_failure or api_error
                if not api_error:
                    observed_models.add(message["model"])
            else:
                observed_models.add(message["model"])
    result = result_rows[0] if len(result_rows) == 1 else {}
    transport_failure = transport_failure or str(result.get("result", "")).startswith(
        "API Error:"
    )
    model_usage = result.get("modelUsage") if isinstance(result.get("modelUsage"), dict) else {}
    model_usage_models = {str(model) for model in model_usage}
    if observed_models != {EXPECTED_MODEL}:
        errors.append("Claude stream model identity does not equal glm-5.2")
    if model_usage_models != {EXPECTED_MODEL}:
        errors.append("Claude modelUsage identity does not equal glm-5.2")
    if transport_failure:
        errors.append("Claude stream reports an API transport failure")
    if result.get("subtype") != "success" or result.get("is_error") is not False:
        errors.append("Claude result is not successful")
    if result.get("stop_reason") != "end_turn":
        errors.append("Claude result did not end with end_turn")

    raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    raw_input = _nonnegative_int(raw_usage.get("input_tokens"), "input_tokens", errors)
    cache_read = _nonnegative_int(
        raw_usage.get("cache_read_input_tokens"), "cache_read_input_tokens", errors
    )
    cache_creation = _nonnegative_int(
        raw_usage.get("cache_creation_input_tokens"), "cache_creation_input_tokens", errors
    )
    output = _nonnegative_int(raw_usage.get("output_tokens"), "output_tokens", errors)
    total_cost = result.get("total_cost_usd")
    if (
        isinstance(total_cost, bool)
        or not isinstance(total_cost, (int, float))
        or not math.isfinite(total_cost)
        or total_cost < 0
    ):
        errors.append("invalid total_cost_usd")
        total_cost = 0.0
    normalized_input = raw_input + cache_read + cache_creation
    executable = Path(executable_path)
    binary_sha256 = executable_sha256 or (_sha256(executable) if executable.is_file() else "")
    if re.fullmatch(r"[0-9a-f]{64}", binary_sha256) is None:
        errors.append("Claude executable identity is invalid")
    success = not errors
    return {
        "schema": SCHEMA,
        "solver": SOLVER,
        "success": success,
        "errors": errors,
        "cli_version": cli_version,
        "stream_cli_version": stream_cli_version,
        "expected_cli_version": EXPECTED_CLI_VERSION,
        "expected_model": EXPECTED_MODEL,
        "observed_models": sorted(observed_models),
        "model_usage_models": sorted(model_usage_models),
        "synthetic_events": synthetic_events,
        "transport_failure": transport_failure,
        "executable": {
            "path": str(executable_path),
            "sha256": binary_sha256,
        },
        "runtime_image": runtime_image or None,
        "runtime_image_id": runtime_image_id or None,
        "expected_runtime_image_id": expected_runtime_image_id or None,
        "task_image_id": task_image_id or None,
        "stream_sha256": _sha256(stream_path) if stream_path.is_file() else None,
        "settings_sha256": _sha256(settings_path) if settings_path.is_file() else None,
        "process_returncode": process_returncode,
        "invocation_binding": binding,
        "result": {
            "subtype": result.get("subtype"),
            "is_error": result.get("is_error"),
            "stop_reason": result.get("stop_reason"),
            "duration_ms": result.get("duration_ms"),
            "duration_api_ms": result.get("duration_api_ms"),
            "num_turns": result.get("num_turns"),
        },
        "usage": {
            "raw_input_tokens": raw_input,
            "input_tokens": normalized_input,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "output_tokens": output,
            "total_tokens": normalized_input + output,
        },
        "cost_usd": float(total_cost),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    settings = subparsers.add_parser("settings")
    settings.add_argument("--base-url", required=True)
    settings.add_argument("--output", type=Path, required=True)
    prompt = subparsers.add_parser("prompt")
    prompt.add_argument("--source", type=Path, required=True)
    prompt.add_argument("--workspace", type=Path, required=True)
    prompt.add_argument("--wrapper", type=Path, required=True)
    prompt.add_argument("--output", type=Path, required=True)
    relay = subparsers.add_parser("relay-url")
    relay.add_argument("--base-url", required=True)
    relay_socket = subparsers.add_parser("relay-socket")
    relay_socket.add_argument("--base-url", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--stream", type=Path, required=True)
    build.add_argument("--settings", type=Path, required=True)
    build.add_argument("--executable", type=Path)
    build.add_argument("--executable-path", default="")
    build.add_argument("--executable-sha256", default="")
    build.add_argument("--cli-version-output", required=True)
    build.add_argument("--process-returncode", type=int, required=True)
    build.add_argument("--runtime-image", default="")
    build.add_argument("--runtime-image-id", default="")
    build.add_argument("--expected-runtime-image-id", default="")
    build.add_argument("--task-image-id", default="")
    build.add_argument("--solver-task-id", default="")
    build.add_argument("--prompt-sha256", default="")
    build.add_argument("--anonymous-head", default="")
    build.add_argument("--base-tree", default="")
    build.add_argument("--raw-patch-sha256", default="")
    build.add_argument("--candidate-tree", default="")
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "settings":
        write_settings(args.output, args.base_url)
        return 0
    if args.command == "prompt":
        write_prompt(args.source, args.output, args.workspace, args.wrapper)
        return 0
    if args.command == "relay-url":
        print(relay_upstream_url(args.base_url))
        return 0
    if args.command == "relay-socket":
        print(relay_socket_path(args.base_url))
        return 0
    payload = build_sidecar(
        stream_path=args.stream,
        settings_path=args.settings,
        executable_path=args.executable or args.executable_path,
        executable_sha256=args.executable_sha256,
        cli_version_output=args.cli_version_output,
        process_returncode=args.process_returncode,
        runtime_image=args.runtime_image,
        runtime_image_id=args.runtime_image_id,
        expected_runtime_image_id=args.expected_runtime_image_id,
        task_image_id=args.task_image_id,
        solver_task_id=args.solver_task_id,
        prompt_sha256=args.prompt_sha256,
        anonymous_head=args.anonymous_head,
        base_tree=args.base_tree,
        raw_patch_sha256=args.raw_patch_sha256,
        candidate_tree=args.candidate_tree,
    )
    _write_json(args.output, payload)
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
