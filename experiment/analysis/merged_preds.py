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
calls the same ``select_cell_rows`` the report calls, reads each row's
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

from opencollab_eval.commands.batch import Batch
from opencollab_eval.commands.batch_reporting import select_cell_rows
from opencollab_eval.engine.swe_eval_record_identity import direct_payload_alias_value, direct_payload_patch_sha
from opencollab_eval.engine.swe_eval_records import MAX_JSONL_SCAN_BYTES
from opencollab_eval.experiment.cell_report import RunRow
from opencollab_eval.safe_files import read_regular_text, write_regular_bytes_atomic


def _preds_path(root: Path, batch_name: str, arm: str) -> Path:
    return root / batch_name / f"preds-{arm}.jsonl"


def _read_preds(path: Path) -> dict[str, list[tuple[dict, str]]]:
    """Keep every prediction and its original JSON representation."""
    out: dict[str, list[tuple[dict, str]]] = {}
    if not path.exists():
        return out
    for line in read_regular_text(path, max_bytes=MAX_JSONL_SCAN_BYTES).splitlines():
        if not line.strip():
            continue
        prediction = json.loads(line)
        out.setdefault(prediction["instance_id"], []).append((prediction, line))
    return out


def _selected_prediction(row: RunRow, predictions: list[tuple[dict, str]]) -> str | None:
    matches = []
    for prediction, line in predictions:
        record_id = direct_payload_alias_value(prediction, ("record_id", "attempt_id", "workflow_record_id"))
        patch_sha = direct_payload_patch_sha(prediction)
        if record_id is None or patch_sha is None:
            continue
        if row.record_id and row.record_id != record_id:
            continue
        if row.patch_sha256 and row.patch_sha256.lower() != patch_sha:
            continue
        matches.append(line)
    if len(matches) > 1:
        raise ValueError(f"ambiguous predictions for {row.instance_id} from {row.source_batch}")
    return matches[0] if matches else None


def build(spec: Path, experiment_dir: Path) -> tuple[list[str], list[tuple[str, str, int, int, str]]]:
    batch = Batch(spec, experiment_dir, None)
    root = Path(batch.host.local_batches_dir)
    rows = select_cell_rows(batch).rows
    cache: dict[str, dict[str, list[tuple[dict, str]]]] = {}
    lines: list[str] = []
    table: list[tuple[str, str, int, int, str]] = []
    missing: list[str] = []
    for row in rows:
        source = row.source_batch or batch.spec.name
        if source not in cache:
            cache[source] = _read_preds(_preds_path(root, source, batch.spec.arm))
        line = _selected_prediction(row, cache[source].get(row.instance_id, []))
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
    parser.add_argument("--check", action="store_true", help="print which out-dir each instance is taken from and stop")
    arguments = parser.parse_args()
    lines, table = build(arguments.spec, arguments.experiment_dir)
    # Count adopted later attempts. A failed retry can retain the original
    # valid prediction even though the instance has multiple attempts.
    superseded = [t for t in table if t[2] > 1]
    print(f"{len(lines)} predictions; {len(superseded)} taken from a later attempt")
    for instance_id, source, attempt, attempts, status in table:
        if attempts > 1 or arguments.check:
            print(f"  {instance_id}  <- {source}  attempt {attempt}/{attempts}  {status}")
    if arguments.check:
        return 0
    if arguments.out is None:
        raise SystemExit("--out is required unless --check")
    write_regular_bytes_atomic(arguments.out, ("\n".join(lines) + "\n").encode("utf-8"))
    print(f"wrote {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
