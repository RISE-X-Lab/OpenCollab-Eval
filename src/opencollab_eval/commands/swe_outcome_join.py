"""Join official SWE-bench grading back onto the runs that produced it.

Generation writes prediction rows; the official harness writes a per-instance
`report.json` under `<work_dir>/logs/run_evaluation/<run_id>/<model>/<instance_id>/`.
Nothing joined the two, so no artifact carried the paper's outcome variable
`y` -- the fraction of an instance's fail-to-pass tests that pass -- next to the
run that earned it.

This walks the harness log tree, computes `y` with
`engine.swe_fail_to_pass_fraction`, and writes one JSONL row per graded run. It
reads only files the harness and this repository already wrote, so a batch that
finished weeks ago is back-filled without re-running anything. The one place
this repository deletes a `report.json` is immediately before a re-run
(`run_swebench_eval_per_instance`), so a finished batch still has all of them.

Identity comes from the `opencollab-attempt.json` sidecar this repository writes
beside each report, which carries `record_id` and `patch_sha256`; the standard
SWE-bench report schema carries no patch identity of its own. When the sidecar
is absent the row falls back to matching on `instance_id` alone, which cannot
tell repetitions of the same task apart. That fallback is labelled in every row
(`identity_source`, `identity_trusted`) and counted in the summary, so the two
provenances can never be pooled by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_fail_to_pass_fraction import (
    fail_to_pass_outcome,
    gold_denominator_check,
    load_instance_report,
)

__all__ = ["build_parser", "collect_outcomes", "main", "summarize"]

OUTCOME_SCHEMA = "opencollab.swe_outcome.v1"
IDENTITY_FROM_SIDECAR = "attempt_sidecar"
IDENTITY_FROM_INSTANCE_ID = "instance_id_fallback"
JOIN_NOT_ATTEMPTED = "not_attempted"
JOIN_MATCHED = "matched"
JOIN_AMBIGUOUS = "ambiguous"
JOIN_UNMATCHED = "unmatched"

MAX_SIDECAR_BYTES = 1024 * 1024
MAX_JSONL_BYTES = 512 * 1024 * 1024


def _read_json_file(path: Path, max_bytes: int) -> Any:
    try:
        if path.stat().st_size > max_bytes:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_JSONL_BYTES:
        raise ValueError(f"file exceeds {MAX_JSONL_BYTES} bytes: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL in {path}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _first_str(row: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


class PredictionIndex:
    """Look up prediction rows by record id, patch digest, or instance id."""

    def __init__(self) -> None:
        self.by_record_id: dict[str, list[dict[str, Any]]] = {}
        self.by_patch_sha256: dict[str, list[dict[str, Any]]] = {}
        self.by_instance_id: dict[str, list[dict[str, Any]]] = {}
        self.row_count = 0

    def add_file(self, path: Path) -> None:
        for line_number, row in enumerate(_read_jsonl(path), start=1):
            located = dict(row)
            located["_prediction_file"] = path.as_posix()
            located["_prediction_line"] = line_number
            self.row_count += 1
            record_id = _first_str(row, ("record_id", "attempt_id", "workflow_record_id"))
            patch_sha = _first_str(row, ("patch_sha256", "patch_sha", "model_patch_sha256"))
            instance_id = _first_str(row, ("instance_id", "task_id", "id"))
            if record_id:
                self.by_record_id.setdefault(record_id, []).append(located)
            if patch_sha:
                self.by_patch_sha256.setdefault(patch_sha, []).append(located)
            if instance_id:
                self.by_instance_id.setdefault(instance_id, []).append(located)

    def lookup(self, key_name: str, value: str) -> list[dict[str, Any]]:
        table = {
            "record_id": self.by_record_id,
            "patch_sha256": self.by_patch_sha256,
            "instance_id": self.by_instance_id,
        }[key_name]
        return table.get(value, [])


def _load_gold_fail_to_pass(paths: Sequence[Path]) -> dict[str, list[str]]:
    gold: dict[str, list[str]] = {}
    for path in paths:
        for row in _read_jsonl(path):
            instance_id = _first_str(row, ("instance_id", "task_id", "id"))
            tests = row.get("FAIL_TO_PASS")
            if isinstance(tests, str):
                try:
                    tests = json.loads(tests)
                except json.JSONDecodeError:
                    tests = None
            if instance_id and isinstance(tests, list):
                gold[instance_id] = [str(test) for test in tests]
    return gold


def discover_report_dirs(
    work_dir: Path,
    run_ids: Sequence[str] = (),
    models: Sequence[str] = (),
) -> list[tuple[str, str, str, Path]]:
    """Yield (run_id, model_dir_name, instance_id, report_path) for every graded instance.

    Mirrors the layout the harness writes. Empty filters mean "everything found",
    which is what a back-fill over a finished batch wants.
    """
    root = work_dir / "logs" / "run_evaluation"
    wanted_runs = set(run_ids)
    wanted_models = {model.replace("/", "__") for model in models}
    found: list[tuple[str, str, str, Path]] = []
    if not root.is_dir():
        return found
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir() or (wanted_runs and run_dir.name not in wanted_runs):
            continue
        for model_dir in sorted(run_dir.iterdir()):
            if not model_dir.is_dir() or (wanted_models and model_dir.name not in wanted_models):
                continue
            for instance_dir in sorted(model_dir.iterdir()):
                report = instance_dir / "report.json"
                if instance_dir.is_dir() and report.is_file():
                    found.append((run_dir.name, model_dir.name, instance_dir.name, report))
    return found


def _sidecar_identity(report_path: Path) -> dict[str, str]:
    payload = _read_json_file(report_path.with_name("opencollab-attempt.json"), MAX_SIDECAR_BYTES)
    if not isinstance(payload, dict):
        return {}
    return {
        "record_id": _first_str(payload, ("record_id",)),
        "patch_sha256": _first_str(payload, ("patch_sha256",)),
        "instance_id": _first_str(payload, ("instance_id",)),
        "attempt_status": _first_str(payload, ("status",)),
    }


def _join_to_predictions(
    index: PredictionIndex | None,
    identity: dict[str, str],
    instance_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if index is None:
        return {"join_key": "", "join_status": JOIN_NOT_ATTEMPTED, "join_candidates": 0}, None
    for key_name in ("record_id", "patch_sha256"):
        value = identity.get(key_name) or ""
        if not value:
            continue
        matches = index.lookup(key_name, value)
        if matches:
            return _join_result(key_name, matches)
    return _join_result("instance_id", index.lookup("instance_id", instance_id))


def _join_result(key_name: str, matches: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not matches:
        status = JOIN_UNMATCHED
    elif len(matches) > 1:
        status = JOIN_AMBIGUOUS
    else:
        status = JOIN_MATCHED
    result: dict[str, Any] = {
        "join_key": key_name,
        "join_status": status,
        "join_candidates": len(matches),
    }
    if status != JOIN_MATCHED:
        return result, None
    result["prediction_file"] = matches[0]["_prediction_file"]
    result["prediction_line"] = matches[0]["_prediction_line"]
    return result, matches[0]


def collect_outcomes(
    work_dir: Path,
    *,
    run_ids: Sequence[str] = (),
    models: Sequence[str] = (),
    index: PredictionIndex | None = None,
    gold_fail_to_pass: dict[str, list[str]] | None = None,
    carry_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Build one outcome row per graded instance found under `work_dir`."""
    rows: list[dict[str, Any]] = []
    for run_id, model_dir_name, instance_id, report_path in discover_report_dirs(work_dir, run_ids, models):
        outcome = fail_to_pass_outcome(load_instance_report(report_path, instance_id))
        identity = _sidecar_identity(report_path)
        from_sidecar = bool(identity.get("record_id") or identity.get("patch_sha256"))
        row: dict[str, Any] = {
            "schema": OUTCOME_SCHEMA,
            "run_id": run_id,
            "model_dir": model_dir_name,
            "instance_id": instance_id,
            "record_id": identity.get("record_id", ""),
            "patch_sha256": identity.get("patch_sha256", ""),
            "attempt_status": identity.get("attempt_status", ""),
            # A row identified only by instance_id cannot tell repetitions of the
            # same task apart, so it never claims the same standing as one bound
            # to a recorded attempt.
            "identity_source": IDENTITY_FROM_SIDECAR if from_sidecar else IDENTITY_FROM_INSTANCE_ID,
            "identity_trusted": from_sidecar,
            "report_path": report_path.as_posix(),
        }
        row.update(outcome.as_dict())
        join, matched = _join_to_predictions(index, identity, instance_id)
        row.update(join)
        gold = (gold_fail_to_pass or {}).get(instance_id)
        check = gold_denominator_check(outcome, gold)
        row["gold_f2p_count"] = check["gold_count"]
        row["gold_denominator_matches"] = check["matches"]
        row["missing_from_denominator"] = check["missing_from_denominator"]
        if matched is not None:
            for field in carry_fields:
                if field in matched:
                    row[f"prediction_{field}"] = matched[field]
        rows.append(row)
    return rows


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the rows, keeping the two means that answer different questions."""
    graded = [row for row in rows if row.get("f2p_graded") is True]
    ungraded_reasons: dict[str, int] = {}
    for row in rows:
        if row.get("f2p_graded") is not True:
            reason = str(row.get("ungraded_reason") or "unknown")
            ungraded_reasons[reason] = ungraded_reasons.get(reason, 0) + 1
    join_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("join_status") or JOIN_NOT_ATTEMPTED)
        join_counts[status] = join_counts.get(status, 0) + 1
    mismatches = [row for row in rows if row.get("gold_denominator_matches") is False]
    return {
        "runs": len(rows),
        "graded": len(graded),
        "ungraded": len(rows) - len(graded),
        "ungraded_reasons": ungraded_reasons,
        # The paper's y is defined over every run, with an ungraded run scoring
        # 0. The graded-only mean is reported beside it because it answers a
        # different question and the two are easy to confuse.
        "mean_y_over_all_runs": (sum(float(row["y"]) for row in rows) / len(rows)) if rows else None,
        "mean_y_over_graded_runs": (sum(float(row["y"]) for row in graded) / len(graded)) if graded else None,
        "resolved": sum(1 for row in rows if row.get("resolved") is True),
        "identity_from_sidecar": sum(1 for row in rows if row.get("identity_trusted") is True),
        "identity_from_instance_id_fallback": sum(1 for row in rows if row.get("identity_trusted") is not True),
        "join_status_counts": join_counts,
        "gold_denominator_mismatches": len(mismatches),
        "gold_denominator_mismatch_instances": sorted({str(row["instance_id"]) for row in mismatches}),
    }


def build_parser(prog: str = "swe-outcome-join") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Compute the graded outcome y from official SWE-bench reports and bind it to run records.",
    )
    parser.add_argument("--work-dir", required=True, type=Path, help="Directory containing logs/run_evaluation")
    parser.add_argument("--run-id", action="append", default=[], help="Restrict to this run id (repeatable)")
    parser.add_argument("--model", action="append", default=[], help="Restrict to this model name (repeatable)")
    parser.add_argument(
        "--predictions",
        action="append",
        default=[],
        type=Path,
        help="Prediction JSONL to join against (repeatable); omit to emit outcomes without a join",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        type=Path,
        help="Dataset JSONL carrying gold FAIL_TO_PASS lists, to check the denominator (repeatable)",
    )
    parser.add_argument(
        "--carry",
        action="append",
        default=[],
        help="Copy this field from the matched prediction row into the outcome row (repeatable)",
    )
    parser.add_argument("--output", required=True, type=Path, help="Destination JSONL for the per-run outcome rows")
    return parser


def main(argv: Sequence[str] | None = None, prog: str = "swe-outcome-join") -> int:
    args = build_parser(prog).parse_args(list(sys.argv[1:] if argv is None else argv))
    index: PredictionIndex | None = None
    try:
        if args.predictions:
            index = PredictionIndex()
            for path in args.predictions:
                index.add_file(path)
        gold = _load_gold_fail_to_pass(args.dataset)
        rows = collect_outcomes(
            args.work_dir,
            run_ids=args.run_id,
            models=args.model,
            index=index,
            gold_fail_to_pass=gold,
            carry_fields=args.carry,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
