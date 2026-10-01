#!/usr/bin/env python3
"""Build the predictions file that holds only what the analyst had written.

Why this exists. In the ``self-collaboration`` arms the three seats share one
tree at ``/testbed``, and the graded patch is whatever ``git diff`` returns at
the end of the run. No seat submits it -- ``submit`` records that an agent
stopped on purpose and carries a text summary, nothing more -- so the record
does not say which seat wrote the thing that was scored.

The workflow does record the whole diff text at each phase boundary
(``workflow_result.tree_snapshots``), which turns the question into a byte
comparison between two recorded artifacts. This script takes the snapshot at
the ``analyze`` boundary -- the tree as the analyst left it, before the coder
was called -- and writes it out in the shape the benchmark harness reads
predictions in, so that "what the analyst alone would have scored" becomes a
scored number rather than an argument about tree diffs.

**The extraction rule, fixed before use.** Take the text after the literal
marker ``[Tracked changes vs HEAD]\\n``; no marker means an empty patch; a
snapshot flagged ``truncated`` is refused rather than written short; a
snapshot whose ``diff`` is ``null`` is an unmeasured run, not an empty one,
and is refused too. Nothing else is rewritten.

**The same rule lives in the paper's ``scripts/numbers.py``** (function
``snapshot_body``), which computes the byte-identity counts. The two must not
drift: if this rule changes, that one changes in the same commit.

**The control this makes available.** Most of the rows written here are
byte-identical to the batch's own submitted patch, because the analyst had
already written the fix. Scoring them again is a positive control on the
scoring session: a harness that returns a different verdict for a
byte-identical patch is not measuring what it is being asked to measure. The
count of identical rows is written to the sidecar so the control has a
number to check against.

Usage
-----
    python3 analyst_patch_preds.py <batch-dir> --arm <arm> --out preds.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The analysis entry point also runs directly from a checkout with python -S.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from opencollab_eval.engine.swe_eval_records import (  # noqa: E402
    latest_paired_rows,
    read_jsonl,
    row_explicit_patch_sha,
    row_patch_sha,
    row_record_id,
    row_task_id,
)
from opencollab_eval.experiment.cell_report_metric_order import report_metric_records  # noqa: E402
from opencollab_eval.experiment.cell_report_rows import run_rows  # noqa: E402
from opencollab_eval.experiment.cell_report_statistics import merge_attempts  # noqa: E402

#: The literal marker ``_snapshot`` writes before the diff body.
SNAPSHOT_MARK = "[Tracked changes vs HEAD]\n"

#: The phase boundary this script reads. ``implement:r1`` is the other one and
#: is what the control in ``numbers.py`` compares against the graded patch.
BOUNDARY = "analyze"


def snapshot_body(diff: str | None) -> str:
    """The patch a snapshot holds. A missing marker means no patch."""
    if not diff:
        return ""
    at = diff.find(SNAPSHOT_MARK)
    return diff[at + len(SNAPSHOT_MARK) :] if at >= 0 else ""


def same_patch(left: str, right: str) -> bool:
    """Byte equality, with the abbreviated ``index`` sha as the one exception."""

    def strip(text: str) -> str:
        return re.sub(r"^index [0-9a-f]+\.\.[0-9a-f]+", "", text, flags=re.M).strip()

    return strip(left) == strip(right)


def build(batch_dir: Path, arm: str) -> tuple[list[dict], dict]:
    """The predictions rows and the sidecar that says what they are.

    Refuses rather than writing a row it cannot stand behind: a truncated
    snapshot would write a short patch that scores as a failed attempt, and a
    null snapshot would write an empty patch that is indistinguishable from a
    run whose analyst wrote nothing.
    """
    metrics = report_metric_records(batch_dir, batch_dir / "metrics.jsonl", arm)
    predictions = read_jsonl(batch_dir / f"preds-{arm}.jsonl")
    if not metrics:
        raise SystemExit(f"{batch_dir}/metrics.jsonl is empty")

    rows: list[dict] = []
    refused: list[dict] = []
    identical = 0
    model = ""
    selected = merge_attempts([(batch_dir.name, run_rows(batch_dir, arm))])
    for chosen in selected:
        instance = chosen.instance_id
        candidates = [prediction for prediction in predictions if row_task_id(prediction) == instance]
        if chosen.record_id:
            candidates = [prediction for prediction in candidates if row_record_id(prediction) == chosen.record_id]
        elif chosen.patch_sha256:
            candidates = [
                prediction for prediction in candidates
                if row_patch_sha(prediction).lower() == chosen.patch_sha256.lower()
            ]
        pair = latest_paired_rows(candidates, metrics, instance)
        # A single historical row without attempt identity still has an unambiguous
        # comparison. Multiple such rows cannot identify the adopted attempt.
        legacy = [
            metric for metric in metrics
            if row_task_id(metric) == instance and not row_record_id(metric) and not row_explicit_patch_sha(metric)
        ]
        run = pair.metric
        if (
            not chosen.record_id and not chosen.patch_sha256 and len(candidates) == 1 and len(legacy) == 1
            and not row_record_id(candidates[0])
        ):
            run = legacy[0]
        if run is None or pair.prediction is None or len(candidates) != 1:
            refused.append({"instance_id": instance, "why": "selected attempt has no unique paired prediction"})
            continue
        graded = pair.prediction.get("model_patch", "")
        model = run.get("llm_model") or model
        snaps = (run.get("workflow_result") or {}).get("tree_snapshots") or []
        snap = next((s for s in snaps if s.get("after") == BOUNDARY), None)
        if snap is None or snap.get("diff") is None:
            refused.append({"instance_id": instance, "why": "snapshot probe returned null"})
            continue
        if snap.get("truncated"):
            refused.append({"instance_id": instance, "why": "snapshot truncated at the cap"})
            continue
        patch = snapshot_body(snap.get("diff"))
        if same_patch(patch, graded):
            identical += 1
        rows.append(
            {
                "instance_id": instance,
                "model_name_or_path": f"analyst-only.{run.get('llm_model') or 'unknown'}",
                "model_patch": patch,
                **({"record_id": chosen.record_id} if chosen.record_id else {}),
            }
        )
    sidecar = {
        "batch": batch_dir.name,
        "arm": arm,
        "boundary": BOUNDARY,
        "model": model,
        "rows": len(rows),
        "rows_with_a_patch": sum(1 for r in rows if r["model_patch"].strip()),
        "identical_to_graded": identical,
        "refused": refused,
        "rule": (
            "text after the literal marker "
            + json.dumps(SNAPSHOT_MARK)
            + "; truncated or null snapshots refused; only the abbreviated index sha "
            "is normalised when comparing"
        ),
    }
    return rows, sidecar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch_dir", type=Path)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows, sidecar = build(args.batch_dir, args.arm)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    side = args.out.with_suffix(args.out.suffix + ".about.json")
    side.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(
        f"{sidecar['rows']} rows -> {args.out}"
        f"  ({sidecar['rows_with_a_patch']} carry a patch,"
        f" {sidecar['identical_to_graded']} identical to the graded patch,"
        f" {len(sidecar['refused'])} refused)"
    )
    for item in sidecar["refused"]:
        print(f"  refused {item['instance_id']}: {item['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
