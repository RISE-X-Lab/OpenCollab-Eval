#!/usr/bin/env python3
# ruff: noqa: F401, F403, F405
"""Run one bounded SWE v1 pro-lite slice and publish its evaluation report."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import types
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval.commands import swe_ssh_transport as _ssh_transport
from opencollab_eval.commands import swe_v1_prolite_common as _common
from opencollab_eval.commands import swe_v1_prolite_config as _config
from opencollab_eval.commands import swe_v1_prolite_controller as _controller
from opencollab_eval.commands import swe_v1_prolite_process as _process
from opencollab_eval.commands import swe_v1_prolite_report as _report
from opencollab_eval.commands import swe_v1_transport_recovery as _transport_recovery
from opencollab_eval.commands.swe_v1_prolite_common import *  # noqa: F403
from opencollab_eval.commands.swe_v1_prolite_config import *  # noqa: F403
from opencollab_eval.commands.swe_v1_prolite_controller import *  # noqa: F403
from opencollab_eval.commands.swe_v1_prolite_process import *  # noqa: F403
from opencollab_eval.commands.swe_v1_prolite_report import *  # noqa: F403
from opencollab_eval.commands.swe_v1_transport_recovery import *  # noqa: F403
from opencollab_eval.engine.solver_backend import KIMI_CODING_BASE_URL, is_kimi_direct_model


def main(*, prog: str | None = None, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Generate and officially evaluate one bounded SWE Pro-Lite slice.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH destination for the Linux worker")  # noqa: F405
    parser.add_argument("--ssh-command", default="ssh", help="SSH executable or command wrapper")
    parser.add_argument("--remote-python", default="python3", help="Worker interpreter used for synchronized modules")
    parser.add_argument(
        "--remote-path-entry",
        action="append",
        default=[],
        help="Additional worker PATH entry, repeatable for multiple entries",
    )
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT, help="Worker evaluation and trusted dataset root")  # noqa: F405
    parser.add_argument("--remote-runtime-repo", default="", help="Worker directory receiving the runtime source tree")
    parser.add_argument("--run-id", default="", help="Run-scoped identity used in reports and ownership records")
    parser.add_argument("--base-run-dir", default="", help="Worker directory for this bounded slice")
    parser.add_argument("--start-index", type=int, default=26, help="One-based first dataset row")
    parser.add_argument("--limit", type=int, default=10, help="Number of consecutive dataset rows")
    parser.add_argument(
        "--workflow",
        default="validation-council-solve",
        help="OpenCollab workflow or external adapter",
    )
    parser.add_argument(
        "--workflow-env",
        action="append",
        default=[],
        help="Allowed workflow KEY=VALUE setting, repeatable for multiple values",
    )
    parser.add_argument("--openhands-command", default="", help="External Solver command template")
    parser.add_argument(
        "--openhands-empty-patch-rejections",
        type=int,
        default=2,
        help="External Solver empty-patch rejections before terminal handling",
    )
    parser.add_argument(
        "--max-empty-patch-retries",
        type=int,
        default=1,
        help="Maximum authorized retries after an empty candidate",
    )
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="Recorded experiment model identity")  # noqa: F405
    parser.add_argument("--llm-model", default="", help="Model identifier sent to the provider")
    parser.add_argument("--llm-provider", default="anthropic", help="OpenCollab provider adapter")
    parser.add_argument("--context-window", type=int, help="Recorded and enforced model context window")
    parser.add_argument("--temperature", type=float, help="Recorded model sampling temperature")
    parser.add_argument("--top-p", type=float, help="Recorded nucleus-sampling value")
    parser.add_argument("--max-output-tokens", type=int, help="Maximum tokens in one model response")
    parser.add_argument(
        "--session-prefix",
        default=DEFAULT_SESSION_PREFIX,  # noqa: F405
        help="Prefix for run-owned remote sessions and containers",
    )
    parser.add_argument(
        "--image-repository",
        default=DEFAULT_IMAGE_REPOSITORY,  # noqa: F405
        help="Repository prefix for SWE task images",
    )
    parser.add_argument(
        "--remote-proxy-base-url",
        default=DEFAULT_REMOTE_PROXY_BASE_URL,  # noqa: F405
        help="Provider or authenticated relay URL visible to the worker",
    )
    parser.add_argument(
        "--local-proxy-base-url",
        default=DEFAULT_LOCAL_PROXY_BASE_URL,  # noqa: F405
        help="Controller relay URL used to establish remote transport",
    )
    parser.add_argument(
        "--proxy-env-file",
        type=Path,  # noqa: F405
        default=DEFAULT_PROXY_ENV_FILE,  # noqa: F405
        help="Protected controller relay environment file",
    )
    parser.add_argument(
        "--remote-api-env-file",
        default="",
        help="Protected worker credential file for direct Kimi transport",
    )
    parser.add_argument("--budget", type=int, default=16_000_000, help="Per-task Solver token budget")
    parser.add_argument("--max-steps", type=int, default=60, help="Maximum Solver steps")
    parser.add_argument("--swe-timeout", type=int, default=14_400, help="Remote generation timeout in seconds")
    parser.add_argument("--task-wall-timeout", type=int, default=15_300, help="Whole-task timeout in seconds")
    parser.add_argument("--eval-timeout", type=int, default=7_200, help="Official evaluation timeout in seconds")
    parser.add_argument("--llm-timeout", type=int, default=900, help="Single model request timeout in seconds")
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=0,
        help="Checkpoint interval in seconds, with zero disabling it",
    )
    parser.add_argument("--max-task-starts", type=int, default=3, help="Maximum Solver starts per task")
    parser.add_argument("--max-eval-attempts", type=int, default=2, help="Maximum official evaluations per candidate")
    parser.add_argument("--eval-only", action="store_true", help="Re-evaluate one explicitly bound existing candidate")
    parser.add_argument("--expected-task", default="", help="Required task ID for an eval-only candidate")
    parser.add_argument("--expected-record-id", default="", help="Required record ID for an eval-only candidate")
    parser.add_argument(
        "--expected-source-patch-sha256",
        default="",
        help="Required source patch SHA-256 for an eval-only candidate",
    )
    parser.add_argument(
        "--expected-eval-patch-sha256",
        default="",
        help=(
            "Optional evaluation patch SHA-256 assertion; the runner recomputes "
            "the canonical value from the bound source patch"
        ),
    )
    parser.add_argument("--eval-dir-name", default="official_eval", help="Official evaluation directory name")
    parser.add_argument("--parent-output-dir", type=Path, help="Bound parent run used by eval-only mode")
    parser.add_argument("--usd-cny", type=float, help="Optional exchange rate for cost reports")
    parser.add_argument(
        "--total-timeout",
        type=int,
        default=240_000,
        help="Bounded remote controller timeout in seconds",
    )
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON, help="Local JSON report path")  # noqa: F405
    parser.add_argument(  # noqa: F405
        "--markdown-output",
        type=Path,  # noqa: F405
        default=DEFAULT_REPORT_MD,  # noqa: F405
        help="Local Markdown report path",
    )
    parser.add_argument("--no-sync-runtime", action="store_true", help="Reuse a previously verified worker runtime")
    parser.add_argument(
        "--expected-runtime-tree-sha256",
        default="",
        help="Required runtime tree identity when synchronization is disabled",
    )
    parser.add_argument(
        "--no-ensure-remote-proxy",
        action="store_true",
        help="Use an externally managed worker relay",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and selection without running tasks",
    )
    args = parser.parse_args(argv)

    if args.eval_only and args.parent_output_dir is None:
        parser.error("--eval-only requires --parent-output-dir")
    if args.eval_only and args.limit != 1:
        parser.error("--eval-only requires --limit 1")

    if args.start_index < 1:
        parser.error("--start-index must be >= 1")
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.limit > MAX_TASKS_PER_RUN:  # noqa: F405
        parser.error(f"--limit must be <= {MAX_TASKS_PER_RUN}")  # noqa: F405
    if args.max_task_starts < 0:
        parser.error("--max-task-starts must be >= 0")
    if args.max_eval_attempts <= 0:
        parser.error("--max-eval-attempts must be > 0")
    if args.openhands_empty_patch_rejections < 0:
        parser.error("--openhands-empty-patch-rejections must be >= 0")
    if args.max_empty_patch_retries < 0:
        parser.error("--max-empty-patch-retries must be >= 0")
    if args.context_window is not None and args.context_window <= 0:
        parser.error("--context-window must be > 0")
    if args.temperature is not None and not 0.0 <= args.temperature <= 2.0:
        parser.error("--temperature must be between 0 and 2")
    if args.top_p is not None and not 0.0 <= args.top_p <= 1.0:
        parser.error("--top-p must be between 0 and 1")
    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be > 0")
    if args.expected_runtime_tree_sha256 and not re.fullmatch(
        r"[0-9a-f]{64}", args.expected_runtime_tree_sha256
    ):
        parser.error("--expected-runtime-tree-sha256 must be a lowercase SHA-256")
    expected_candidate_binding = (
        args.expected_task,
        args.expected_record_id,
        args.expected_source_patch_sha256,
    )
    expected_candidate_fields = (
        *expected_candidate_binding,
        args.expected_eval_patch_sha256,
    )
    if any(expected_candidate_fields) and not all(expected_candidate_binding):
        parser.error(
            "eval-only candidate identity requires task, record ID, and source patch SHA-256"
        )
    if any(expected_candidate_fields) and not args.eval_only:
        parser.error("expected candidate identity is supported only with --eval-only")
    for option, value in (("--expected-task", args.expected_task), ("--expected-record-id", args.expected_record_id)):
        if value and (len(value.encode("utf-8")) > 256 or any(ord(character) < 32 for character in value)):
            parser.error(f"{option} is invalid")
    for option, value in (
        ("--expected-source-patch-sha256", args.expected_source_patch_sha256),
        ("--expected-eval-patch-sha256", args.expected_eval_patch_sha256),
    ):
        if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            parser.error(f"{option} must be a lowercase SHA-256")
    if args.no_sync_runtime and not args.expected_runtime_tree_sha256:
        parser.error("--no-sync-runtime requires --expected-runtime-tree-sha256")
    try:
        normalize_workflow_env(args.workflow_env)  # noqa: F405
    except ValueError as exc:
        parser.error(str(exc))
    positive_values = {
        "--budget": args.budget,
        "--max-steps": args.max_steps,
        "--swe-timeout": args.swe_timeout,
        "--task-wall-timeout": args.task_wall_timeout,
        "--eval-timeout": args.eval_timeout,
        "--llm-timeout": args.llm_timeout,
        "--total-timeout": args.total_timeout,
    }
    for option, value in positive_values.items():
        if value <= 0:
            parser.error(f"{option} must be > 0")
    if not args.eval_only and args.checkpoint_interval != 0:
        parser.error("--checkpoint-interval must be 0 for trusted host extraction")
    if args.remote_api_env_file and (
        args.llm_provider != "openai" or not is_kimi_direct_model(args.llm_model)
    ):
        parser.error("--remote-api-env-file is supported only for direct Kimi models")
    if args.remote_api_env_file and args.remote_proxy_base_url.rstrip("/") != KIMI_CODING_BASE_URL:
        parser.error(f"Kimi direct mode requires --remote-proxy-base-url {KIMI_CODING_BASE_URL}")
    if args.run_id:
        try:
            args.run_id = validate_run_id(args.run_id)  # noqa: F405
        except ValueError as exc:
            parser.error(str(exc))

    required = {
        "--host or OPENCOLLAB_SWE_HOST": args.host,
        "--remote-root or OPENCOLLAB_SWE_REMOTE_ROOT": args.remote_root,
        "--model-name or OPENCOLLAB_SWE_MODEL_NAME": args.model_name,
        "--remote-python": args.remote_python,
        "--session-prefix or OPENCOLLAB_SWE_SESSION_PREFIX": args.session_prefix,
        "--image-repository or OPENCOLLAB_SWE_IMAGE_REPOSITORY": args.image_repository,
        ("--remote-proxy-base-url or OPENCOLLAB_REMOTE_PROXY_BASE_URL"): args.remote_proxy_base_url,
    }
    for option, value in required.items():
        if not str(value or "").strip():
            parser.error(f"{option} is required")
    if (
        not args.eval_only
        and not str(args.remote_api_env_file or "").strip()
        and not args.no_ensure_remote_proxy
        and not str(args.local_proxy_base_url or "").strip()
    ):
        parser.error(
            "--local-proxy-base-url or OPENCOLLAB_LOCAL_PROXY_BASE_URL is required when remote proxy setup is enabled"
        )

    configure_run_paths(args)  # noqa: F405
    if args.eval_only:
        with parent_eval_lock(args):  # noqa: F405
            parent_eval_budget = apply_parent_eval_budget(args)  # noqa: F405
            try:
                summary = run_remote(args)  # noqa: F405
            except KeyboardInterrupt:
                return 130
            write_local_report(  # noqa: F405
                summary,
                args.json_output,
                args.markdown_output,
            )
            summary["parent_eval_budget"] = parent_eval_budget
            with parent_report_lock(args):  # noqa: F405
                summary["parent_fact_report"] = update_parent_fact_report(args)  # noqa: F405
            write_local_report(  # noqa: F405
                summary,
                args.json_output,
                args.markdown_output,
            )
    else:
        try:
            summary = run_remote(args)  # noqa: F405
        except KeyboardInterrupt:
            return 130
        write_local_report(  # noqa: F405
            summary,
            args.json_output,
            args.markdown_output,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"done", "dry_run"} else 1


_COMPATIBILITY_MODULES = (
    _common,
    _process,
    _config,
    _controller,
    _report,
    _ssh_transport,
    _transport_recovery,
)


class _CompatibilityModule(types.ModuleType):
    """Propagate legacy module monkeypatches to the extracted implementation."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule


if __name__ == "__main__":
    raise SystemExit(main())
