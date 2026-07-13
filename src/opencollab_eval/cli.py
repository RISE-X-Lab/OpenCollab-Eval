"""Command-line entrypoint for OpenCollab-Eval."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from opencollab_eval import __version__
from opencollab_eval.benchmarks.swe_batch_pro import load_identity_key, load_jsonl_dataset, tasks_from_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oc-eval")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a SWE-Batch Pro JSONL dataset")
    inspect_parser.add_argument("dataset", type=Path)
    inspect_parser.add_argument("--identity-key-file", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        identity_key = load_identity_key(args.identity_key_file)
        tasks = tasks_from_rows(load_jsonl_dataset(args.dataset), identity_key=identity_key)
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
