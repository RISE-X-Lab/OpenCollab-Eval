"""Retry, replacement, and withdrawal selection shared by reports and predictions."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencollab_eval.engine.swe_eval_records import MAX_JSON_DOCUMENT_BYTES
from opencollab_eval.experiment import cell_report
from opencollab_eval.experiment.batch_spec import SpecError, suite_rows
from opencollab_eval.generation.gen_prediction_batch import DELIVERY_READABLE_ARMS
from opencollab_eval.safe_files import read_regular_text, write_regular_bytes_atomic

if TYPE_CHECKING:
    from opencollab_eval.commands.batch import Batch, Ssh


def batch_records(root: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Every planned batch under ``root`` as ``(first launch time, name, spec)``.

    Read from the records rather than from a naming convention: the record is
    what ``plan`` checked, and a directory named ``<something>-retry`` that no
    record backs is not a second attempt at anything.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for record_path in sorted(root.glob("*.launch/batch.json")):
        try:
            record = json.loads(read_regular_text(record_path, max_bytes=MAX_JSON_DOCUMENT_BYTES))
        except (OSError, ValueError):
            continue
        spec = record.get("spec") or {}
        name = str(spec.get("name") or record_path.parent.name.removesuffix(".launch"))
        launches = record.get("launches") or []
        at = str((launches[0] or {}).get("at") or "") if launches else ""
        found.append((at, name, spec))
    return sorted(found)


def retry_batches(root: Path, name: str) -> list[tuple[str, Path]]:
    """All recorded descendants of ``name``, in launch and dependency order.

    A child is an attempt after its parent. Among available children, first
    launch time determines which was started next.
    """
    children: dict[str, list[tuple[str, str]]] = {}
    for at, child, spec in batch_records(root):
        parent = spec.get("retry_of")
        if parent:
            children.setdefault(str(parent), []).append((at, child))

    active: set[str] = set()
    visited: set[str] = set()

    def check_cycle(parent: str) -> None:
        if parent in active:
            raise SpecError(f"retry_of cycle reaches {parent!r} from {name!r}")
        if parent in visited:
            return
        active.add(parent)
        for _, child in children.get(parent, []):
            check_cycle(child)
        active.remove(parent)
        visited.add(parent)

    check_cycle(name)
    ready = list(children.get(name, []))
    ordered: list[tuple[str, Path]] = []
    while ready:
        ready.sort()
        _, child = ready.pop(0)
        ordered.append((child, root / child))
        ready.extend(children.get(child, []))
    return ordered


def replacement_batches(batch: Batch) -> list[tuple[str, Path, str, str]]:
    """The out-dirs standing in for an instance of this cell, oldest first.

    Each is ``(name, out-dir, the instance it replaces, the instance it runs)``.
    The instance it runs is resolved from the record's own suite and rows, not
    from its metrics, so the reason a replaced run left the denominators can be
    written even before the replacement has been pulled.
    """
    root = Path(batch.host.local_batches_dir)
    found: list[tuple[str, Path, str, str]] = []
    claimed: dict[str, str] = {}
    for _, name, spec in batch_records(root):
        replaces = spec.get("replaces") or {}
        if replaces.get("batch") != batch.spec.name:
            continue
        gone = str(replaces.get("instance") or "")
        if previous := claimed.get(gone):
            raise SpecError(f"replaces {batch.spec.name!r}: {gone} is claimed by both {previous!r} and {name!r}")
        claimed[gone] = name
        try:
            from opencollab_eval.commands.batch import original_slice

            rows = suite_rows(original_slice(batch.spec, spec), batch.suite_dir)
        except SpecError as exc:
            print(f"  replacement {name}: its slice cannot be read ({exc}); not merged")
            continue
        if len(rows) != 1:
            print(f"  replacement {name}: its slice is {len(rows)} instances, not one; not merged")
            continue
        found.append((name, root / name, gone, rows[0]["instance_id"]))
    return found


def _cell_rows(batch: Batch, name: str, data_dir: Path) -> list[cell_report.RunRow]:
    """One row per instance of one out-dir, with that out-dir's retries folded in."""
    root = Path(batch.host.local_batches_dir)
    attempts = [(name, cell_report.run_rows(data_dir, batch.spec.arm))]
    for retry_name, retry_dir in retry_batches(root, name):
        if not (retry_dir / "metrics.jsonl").exists():
            print(f"  retry {retry_name}: planned but not pulled ({retry_dir}/metrics.jsonl missing); not merged")
            continue
        attempts.append((retry_name, cell_report.run_rows(retry_dir, batch.spec.arm)))
    return cell_report.merge_attempts(attempts)


@dataclass
class CellSelection:
    """The observed runs retained by the same retry and replacement policy."""

    rows: list[cell_report.RunRow]
    missing: list[str]
    excluded: list[tuple[cell_report.RunRow, str]]
    withdrawn: list[tuple[str, str, str, str]]
    replacement_batches: list[str]


def select_cell_rows(batch: Batch) -> CellSelection:
    rows = _cell_rows(batch, batch.spec.name, batch.data_dir)

    # A replaced instance leaves the cell only once something has taken its
    # place. Dropping it the moment a replacement spec exists would shrink the
    # denominator before the replacement had run.
    replaced: dict[str, str] = {}
    stand_ins: list[cell_report.RunRow] = []
    merged_replacements: list[str] = []
    for name, data_dir, gone, arrives in replacement_batches(batch):
        if not (data_dir / "metrics.jsonl").exists():
            print(f"  replacement {name}: planned but not pulled ({data_dir}/metrics.jsonl missing); not merged")
            continue
        for row in _cell_rows(batch, name, data_dir):
            row.replacement_for = gone
            stand_ins.append(row)
        replaced[gone] = arrives
        merged_replacements.append(name)
    # A withdrawal turns the merge around. The fault that triggered the
    # replacement was not the instance's, so the instance the cell drew goes
    # back into every denominator and the stand-in's run -- paid for, and now
    # counted by nothing -- takes the place in ``excluded`` the drawn run had.
    # Both halves stay in the document: "withdrawn" is not "never happened",
    # and that is the whole reason this is a spec field the report reads rather
    # than an edit to a report JSON.
    withdrawals = batch.spec.withdraw_replacements
    unknown = sorted(set(withdrawals) - set(replaced))
    if unknown:
        raise SpecError(
            f"withdraw_replacements names {unknown}, which no replacement merged into "
            f"{batch.spec.name} stands in for (merged: {sorted(replaced) or 'none'}). A withdrawal "
            "that matches nothing would leave the stand-in in the cell without saying so."
        )
    excluded: list[tuple[cell_report.RunRow, str]] = []
    withdrawn: list[tuple[str, str, str, str]] = []
    if withdrawals:
        for row in stand_ins:
            why = withdrawals.get(row.replacement_for)
            if why is None:
                continue
            excluded.append((row, cell_report.WITHDRAWN_REASON.format(instance=row.replacement_for, why=why)))
            withdrawn.append((row.replacement_for, row.instance_id, row.source_batch, why))
        stand_ins = [r for r in stand_ins if r.replacement_for not in withdrawals]
        replaced = {gone: arrives for gone, arrives in replaced.items() if gone not in withdrawals}
    if replaced:
        kept = []
        for row in rows:
            if row.instance_id in replaced:
                excluded.append((row, cell_report.REPLACED_REASON.format(instance=replaced[row.instance_id])))
            else:
                kept.append(row)
        rows = kept + stand_ins

    suite_file = batch.suite_dir / f"{batch.spec.suite}.csv"
    ordered, missing = cell_report.order_rows(rows, suite_file)
    wanted = ({row["instance_id"] for row in batch.rows} - set(replaced)) | {r.instance_id for r in stand_ins}
    ordered = [r for r in ordered if r.instance_id in wanted]
    missing = [i for i in missing if i in wanted]
    return CellSelection(ordered, missing, excluded, withdrawn, merged_replacements)


def cmd_report(batch: Batch, _remote: Ssh | None, scanner: str | None, json_out: Path | None) -> int:
    root = Path(batch.host.local_batches_dir)
    selection = select_cell_rows(batch)
    ordered, missing = selection.rows, selection.missing
    excluded, withdrawn = selection.excluded, selection.withdrawn
    merged_replacements = selection.replacement_batches
    expected = None
    record_path = batch.record_path()
    if record_path.exists():
        try:
            expected = (
                (json.loads(read_regular_text(record_path, max_bytes=MAX_JSON_DOCUMENT_BYTES)).get("host") or {}).get(
                    "role_prompt_sha256"
                )
                or {}
            ).get("analyst")
        except ValueError:
            expected = None
    # Two different questions, and they used to be one flag. ``team`` is "does
    # this arm seat more than one role worth laying out", which is wider than
    # the arms that take a team file: a DW run has an analyst, a coder and a
    # tester too, and reporting it with ``team=False`` printed null delivery,
    # null alpha and null CI for the arm the ladder compares.
    # ``alpha_readable`` is the narrower "is this arm's delivery rate alpha",
    # which the scripted workflow fails: its edges are sequenced by
    # ``self_collaboration.py``, so a rate there measures the script.
    summary = cell_report.summarize(
        ordered,
        expected,
        team=batch.spec.arm in DELIVERY_READABLE_ARMS,
        alpha_readable=batch.spec.arm in cell_report.ALPHA_READABLE_ARMS,
        timeout_s=batch.spec.timeout,
        withdrawn=withdrawn,
    )
    label = f"{batch.spec.arm}/{batch.spec.cell}" + (f" (rung {batch.spec.rung})" if batch.spec.rung else "")
    print(f"report for {batch.spec.name} ({label}) from {batch.data_dir}")
    for name, _dir in retry_batches(root, batch.spec.name):
        if (root / name / "metrics.jsonl").exists():
            print(f"  merged retry: {name} from {root / name}")
    for name in merged_replacements:
        print(f"  merged replacement: {name} from {root / name}")
    for gone, stands_in, source, _why in withdrawn:
        print(f"  withdrawn replacement: {stands_in} (from {source}) no longer stands in for {gone}")
    pins = batch.spec.pins
    print(f"  pins: opencollab {pins['opencollab'][:12]} opencollab_eval {pins['opencollab_eval'][:12]}")
    print(cell_report.render(ordered, summary, missing))
    if json_out:
        write_regular_bytes_atomic(
            json_out,
            (
                json.dumps(
                    cell_report.report_document(ordered, summary, missing, excluded), indent=2, ensure_ascii=False
                )
                + "\n"
            ).encode("utf-8"),
        )
        print(f"JSON -> {json_out}")
    scanner = None if scanner == "none" else (scanner or batch.host.scanner)
    if scanner:
        print(f"\n--- scan_batch ({scanner}) ---")
        sys.stdout.flush()
        subprocess.run([sys.executable, scanner, str(batch.data_dir)], check=False)
    return 0
