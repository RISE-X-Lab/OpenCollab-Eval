#!/usr/bin/env python3
"""The predictions a cell actually stands behind: one per instance, from the attempt that counts.

A cell whose rows were re-run keeps every attempt in its own out-dir, and the
report reads an instance's **last attempt that ran** -- the last whose status is
not in ``INVALID_STATUSES``. The prediction files do not merge themselves: the
original out-dir still holds the row the failed attempt wrote, patch and all.
Scoring `preds-<arm>.jsonl` straight out of the original directory therefore
scores the attempt the report already discarded, silently, with no count out of
place to notice it by.

This writes the merged file instead, and it does not re-implement the rule: it
calls the same ``_cell_rows`` the report calls, reads each row's
``source_batch``, and copies that instance's line out of that out-dir. If the
two ever diverge, they diverge together.

Usage:
    python3 experiment/analysis/merged_preds.py <spec.yaml> --out <merged.jsonl>
    python3 experiment/analysis/merged_preds.py <spec.yaml> --check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opencollab_eval.commands.batch import Batch, _cell_rows


def _preds_path(root: Path, batch_name: str, arm: str) -> Path:
    return root / batch_name / f"preds-{arm}.jsonl"


def _read_preds(path: Path) -> dict[str, str]:
    """instance_id -> the raw JSON line, so nothing is reformatted on the way through."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            out[json.loads(line)["instance_id"]] = line
    return out


def build(spec: Path, experiment_dir: Path) -> tuple[list[str], list[tuple[str, str, int, int, str]]]:
    batch = Batch(spec, experiment_dir, None)
    root = Path(batch.host.local_batches_dir)
    rows = _cell_rows(batch, batch.spec.name, batch.data_dir)
    cache: dict[str, dict[str, str]] = {}
    lines: list[str] = []
    table: list[tuple[str, str, int, int, str]] = []
    missing: list[str] = []
    for row in rows:
        source = row.source_batch or batch.spec.name
        if source not in cache:
            cache[source] = _read_preds(_preds_path(root, source, batch.spec.arm))
        line = cache[source].get(row.instance_id)
        if line is None:
            missing.append(f"{row.instance_id} (looked in {source})")
            continue
        lines.append(line)
        table.append((row.instance_id, source, row.attempt, row.attempts, row.status))
    if missing:
        raise SystemExit("no prediction row for: " + "; ".join(missing))
    return lines, table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--experiment-dir", type=Path, default=Path("experiment"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true",
                        help="print which out-dir each instance is taken from and stop")
    arguments = parser.parse_args()
    lines, table = build(arguments.spec, arguments.experiment_dir)
    # An instance that was re-run has more than one attempt. Asking instead
    # whether the chosen attempt is the *last* one answers "yes" for every row
    # -- the rule picks the last attempt that ran -- and so reports a merged
    # file as if nothing had been merged. That predicate was written here once
    # and printed "0 taken from a later attempt" for a cell with seven.
    superseded = [t for t in table if t[3] > 1]
    print(f"{len(lines)} predictions; {len(superseded)} taken from a later attempt")
    for instance_id, source, attempt, attempts, status in table:
        if attempts > 1 or arguments.check:
            print(f"  {instance_id}  <- {source}  attempt {attempt}/{attempts}  {status}")
    if arguments.check:
        return 0
    if arguments.out is None:
        raise SystemExit("--out is required unless --check")
    arguments.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
