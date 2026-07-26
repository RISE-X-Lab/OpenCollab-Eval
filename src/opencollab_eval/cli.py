"""Command-line entrypoint for OpenCollab-Eval."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval import __version__
from opencollab_eval.benchmarks.swe_batch_pro import load_identity_key, load_jsonl_dataset, tasks_from_rows
from opencollab_eval.commands.eval_batch import _eval, _result_counts
from opencollab_eval.commands.swe_final_report import add_arguments as add_final_report_arguments
from opencollab_eval.commands.swe_final_report import run_from_args as run_final_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oc-eval",
        description=(
            "Inspect benchmark inputs, generate evidence-bound candidates, "
            "run bounded official SWE evaluation, and publish validated reports."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a SWE-Batch Pro JSONL dataset")
    inspect_parser.add_argument("dataset", type=Path, help="Bounded benchmark JSONL file")
    inspect_parser.add_argument(
        "--identity-key-file",
        required=True,
        type=Path,
        help="Evaluator-owned file containing exactly 32 random bytes",
    )
    inspect_parser.add_argument(
        "--image-repository",
        default=os.environ.get("OPENCOLLAB_SWE_IMAGE_REPOSITORY"),
        help="Repository prefix for rows that provide only dockerhub_tag",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Generate candidates from generic task JSONL and report submission eligibility",
    )
    run_parser.add_argument("tasks_file", type=Path, help="Generic evaluator task JSONL file")
    run_parser.add_argument("--model", default=os.environ.get("OPENCOLLAB_MODEL"), help="Provider model identifier")
    run_parser.add_argument(
        "--provider",
        default=os.environ.get("OPENCOLLAB_PROVIDER"),
        help="OpenCollab provider adapter",
    )
    run_parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENCOLLAB_API_KEY"),
        help="Provider credential, preferably supplied through OPENCOLLAB_API_KEY",
    )
    run_parser.add_argument("--base-url", default=os.environ.get("OPENCOLLAB_BASE_URL"), help="Provider base URL")
    run_parser.add_argument("--output", type=Path, default=Path("eval_results"), help="Candidate result directory")
    run_parser.add_argument("--concurrency", type=int, default=4, help="Maximum concurrent tasks")
    run_parser.add_argument("--max-tokens", type=int, default=1_000_000, help="Default task token budget")
    run_parser.add_argument("--timeout", type=float, default=600.0, help="Default task timeout in seconds")
    run_parser.add_argument("--temperature", type=float, default=0.2, help="Model sampling temperature")
    run_parser.add_argument("--top-p", type=float, help="Optional nucleus-sampling value")
    final_parser = subparsers.add_parser(
        "final-report",
        help="Build a final comparison report from two terminal SWE fact reports",
    )
    add_final_report_arguments(final_parser)
    subparsers.add_parser(
        "swe-v1-prolite",
        help="Run one bounded SWE Pro-Lite slice through the remote production runner",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["swe-v1-prolite"]:
        from opencollab_eval.commands.swe_v1_prolite_runner import main as run_swe_v1_prolite

        return run_swe_v1_prolite(prog="oc-eval swe-v1-prolite", argv=arguments[1:])
    args = build_parser().parse_args(arguments)
    if args.command == "inspect":
        identity_key = load_identity_key(args.identity_key_file)
        tasks = tasks_from_rows(
            load_jsonl_dataset(args.dataset),
            identity_key=identity_key,
            image_repository=args.image_repository,
        )
        print(
            json.dumps(
                {
                    "count": len(tasks),
                    "public_task_ids": [task.public.task_id for task in tasks],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run":
        if not args.model or not args.provider:
            raise SystemExit("run requires --model and --provider (or matching OPENCOLLAB_* variables)")
        results = asyncio.run(
            _eval(
                tasks_file=str(args.tasks_file),
                model=args.model,
                provider=args.provider,
                api_key=args.api_key,
                base_url=args.base_url,
                output_dir=str(args.output),
                concurrency=args.concurrency,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                temperature=args.temperature,
                top_p=args.top_p,
            )
        )
        eligible, ineligible = _result_counts(results)
        print(json.dumps({"tasks": len(results), "eligible_patches": eligible, "ineligible": ineligible}))
        return 0
    if args.command == "final-report":
        try:
            result = run_final_report(args)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


__all__ = ["build_parser", "main"]
