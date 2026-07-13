"""Command-line entrypoint for OpenCollab-Eval."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval import __version__
from opencollab_eval.benchmarks.swe_batch_pro import load_jsonl_dataset, task_from_row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oc-eval")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a SWE-Batch Pro JSONL dataset")
    inspect_parser.add_argument("dataset", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        tasks = [task_from_row(row) for row in load_jsonl_dataset(args.dataset)]
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
    raise AssertionError(f"unhandled command: {args.command}")


__all__ = ["build_parser", "main"]

