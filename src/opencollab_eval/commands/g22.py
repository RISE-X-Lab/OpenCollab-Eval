"""Run G22 through the existing official evaluator from one configuration file."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencollab_eval.commands import swe_g11_parallel_runner as parallel
from opencollab_eval.runtime_config import SINGLE2_AUTHORIZED_BUDGET, SINGLE2_AUTHORIZED_MAX_STEPS

WORKFLOW = "validation-council-dual-coder-selection-v2"
DEFAULT_WORKFLOW_ENV = {
    "OPENCOLLAB_WIRE_PROTOCOL": "responses",
    "OPENCOLLAB_REASONING_EFFORT": "max",
    "OPENCOLLAB_UNBOUNDED_LIMITS": "true",
    "OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION": "1",
    "OPENCOLLAB_EVAL_NO_PROGRESS_TIMEOUT": "43200",
}


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return os.path.expandvars(str(value))
    raise ValueError("configuration values must be strings, numbers, booleans, or lists")


def resolve_config(path: Path, overrides: dict[str, Any] | None = None) -> parallel.ParallelConfig:
    """Translate JSON settings into the established parallel-runner arguments."""
    path = path.expanduser().resolve()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("G22 configuration must be a JSON object")
    data.update({key: value for key, value in (overrides or {}).items() if value is not None})
    if data.get("workflow", WORKFLOW) != WORKFLOW:
        raise ValueError(f"g22 uses workflow {WORKFLOW}")
    settings = {
        "runner_transport": "local",
        "workflow": WORKFLOW,
        "agent_profile": "single2",
        "llm_provider": "openai",
        "max_workers": 1,
        "max_task_starts": 1,
        "max_empty_patch_retries": 0,
        "no_ensure_remote_proxy": True,
        "budget": SINGLE2_AUTHORIZED_BUDGET,
        "max_steps": SINGLE2_AUTHORIZED_MAX_STEPS,
        **data,
    }
    settings.setdefault("run_id", "g22_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f"))
    run_id = str(settings["run_id"])
    if not settings.get("indices") and not any(key in settings for key in ("start_index", "end_index")):
        settings["indices"] = "1"
    root = str(settings.get("remote_root", "")).rstrip("/")
    if root:
        settings.setdefault("remote_eval_work_root", root + "/runs")
    settings.setdefault("output_dir", str(path.parent / "results" / run_id))
    settings.setdefault("remote_python", sys.executable if settings["runner_transport"] == "local" else "python3")
    settings.setdefault("model_name", settings.get("llm_model", ""))
    if settings["runner_transport"] == "local":
        settings.setdefault("local_proxy_base_url", settings.get("remote_proxy_base_url", ""))

    environment = dict(DEFAULT_WORKFLOW_ENV)
    configured_env = settings.pop("workflow_env", {})
    if isinstance(configured_env, list):
        explicit = {}
        for item in configured_env:
            if not isinstance(item, str) or "=" not in item:
                raise ValueError("workflow_env entries must have KEY=VALUE form")
            key, value = item.split("=", 1)
            explicit[key] = value
        configured_env = explicit
    if not isinstance(configured_env, dict):
        raise ValueError("workflow_env must be an object or a list of KEY=VALUE settings")
    environment.update({key: _value(value) for key, value in configured_env.items()})
    settings["workflow_env"] = [f"{key}={value}" for key, value in environment.items()]

    parser = parallel.build_parser()
    actions = {action.dest: action for action in parser._actions if action.dest != "help"}
    unknown = set(settings) - set(actions)
    if unknown:
        raise ValueError("unknown G22 configuration keys: " + ", ".join(sorted(unknown)))
    arguments = []
    for key, value in settings.items():
        if value is None:
            continue
        action = actions[key]
        flag = action.option_strings[0]
        if isinstance(action, argparse._StoreTrueAction):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a JSON boolean")
            if value:
                arguments.append(flag)
        elif isinstance(action, argparse._AppendAction):
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
            for item in value:
                arguments.extend((flag, _value(item)))
        else:
            arguments.extend((flag, _value(value)))
    return parallel.resolve_config(parser.parse_args(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oc-eval g22", description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON configuration outside the source checkout")
    parser.add_argument("--indices", help="One-based task indices and inclusive ranges, such as 1 or 2-50")
    parser.add_argument("--workers", type=int, help="Concurrent task slots")
    parser.add_argument("--run-id", help="Run identity; reuse the same value to resume existing results")
    parser.add_argument("--output-dir", type=Path, help="Directory for controller reports and trajectories")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print effective configuration without model or Docker calls",
    )
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args.config, {
            "indices": args.indices, "max_workers": args.workers, "run_id": args.run_id,
            "output_dir": str(args.output_dir) if args.output_dir is not None else None,
        })
        if args.dry_run:
            print(json.dumps({"status": "configuration_validated", "model_calls": 0,
                              "configuration": asdict(config)}, default=str, ensure_ascii=False, indent=2))
            return 0
        final = parallel.run_parallel(config)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(parallel.compact_progress(final), ensure_ascii=False, indent=2))
    return 0 if final["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
