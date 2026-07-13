"""Build a fail-closed final comparison report from two terminal SWE fact reports."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_report_io as report_io
from opencollab_eval.commands.swe_final_report_dataset import (
    DatasetInputError,
    load_dataset_census,
)
from opencollab_eval.commands.swe_final_report_model import (
    LABELS_SCHEMA,
    NARRATIVE_SCHEMA,
    FinalReportInputError,
    build_comparison,
    load_audit_manifest,
    load_method_facts,
    load_optional_document,
)
from opencollab_eval.commands.swe_final_report_render import render_markdown, render_tex

_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
_METHOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .+_-]{0,63}\Z")
_DATASET_ID = "swe-batch-pro-lite"


def _nonempty(value: str, *, label: str) -> str:
    text = value.strip()
    if not text:
        raise FinalReportInputError(f"{label} must not be empty")
    return text


def _meeting_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise FinalReportInputError("meeting date must use YYYY-MM-DD") from exc


def _safe_output_target(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise FinalReportInputError(f"output target is not a regular file: {path}")


def _publish(staged: Path, target: Path) -> None:
    _safe_output_target(target)
    os.replace(staged, target)


def _publish_transaction(
    *,
    stage: Path,
    staged_outputs: dict[str, Path],
    hashes: dict[str, str],
    output_dir: Path,
    manifest_path: Path,
    final_manifest: dict[str, Any],
) -> dict[str, str]:
    """Replace one report set and restore every previous artifact on failure."""

    order = ("pdf", "tex", "markdown", "json")
    targets = {name: output_dir / staged_outputs[name].name for name in order}
    for target in targets.values():
        _safe_output_target(target)
    backups: dict[str, Path] = {}
    published: dict[str, str] = {}
    try:
        for name in order:
            target = targets[name]
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            backup = stage / f".previous-{name}"
            os.replace(target, backup)
            backups[name] = backup
        for name in order:
            _publish(staged_outputs[name], targets[name])
            published[name] = str(targets[name])
        for name, target in targets.items():
            if _file_sha256(target) != hashes[name]:
                raise FinalReportInputError(f"published {name} changed before manifest finalization")
        _write_status_manifest(manifest_path, final_manifest)
        return published
    except Exception as exc:
        rollback_errors: list[str] = []
        for name in reversed(order):
            target = targets[name]
            try:
                target.lstat()
            except FileNotFoundError:
                pass
            else:
                try:
                    _safe_output_target(target)
                    os.replace(target, stage / f".failed-{name}")
                except Exception as rollback_exc:
                    rollback_errors.append(f"remove {name}: {rollback_exc}")
            backup = backups.get(name)
            if backup is not None:
                try:
                    os.replace(backup, target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"restore {name}: {rollback_exc}")
        if rollback_errors:
            raise FinalReportInputError("report publication rollback failed: " + "; ".join(rollback_errors)) from exc
        raise


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(report_io.read_bytes(path)).hexdigest()


def _write_status_manifest(path: Path, value: dict[str, Any]) -> None:
    report_io.write_json(path, value)


def _compile_pdf(
    tex_path: Path,
    *,
    output_dir: Path,
    latex_engine: str,
    timeout_seconds: float,
) -> tuple[Path, str]:
    engine = shutil.which(latex_engine)
    if engine is None:
        raise FinalReportInputError(f"LaTeX engine is unavailable: {latex_engine}")
    command = [
        engine,
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={output_dir}",
        str(tex_path),
    ]
    logs: list[str] = []
    for _ in range(2):
        try:
            result = subprocess.run(
                command,
                cwd=output_dir,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FinalReportInputError("LaTeX compilation timed out") from exc
        logs.append(result.stdout + result.stderr)
        if result.returncode != 0:
            detail = (result.stdout + result.stderr)[-4000:]
            raise FinalReportInputError(f"LaTeX compilation failed: {detail}")
    pdf_path = output_dir / f"{tex_path.stem}.pdf"
    try:
        pdf_stat = pdf_path.lstat()
    except FileNotFoundError as exc:
        raise FinalReportInputError("LaTeX reported success without a PDF") from exc
    if not stat.S_ISREG(pdf_stat.st_mode) or pdf_stat.st_size < 1024:
        raise FinalReportInputError("LaTeX produced an invalid or empty PDF")
    return pdf_path, "\n".join(logs)


def _default_prefix(meeting_date: str) -> str:
    return f"g11_openhands_prolite_1_100_final_comparison_{meeting_date.replace('-', '')}"


@contextmanager
def _publication_lock(args: argparse.Namespace) -> Iterator[None]:
    """Serialize writers for one output prefix without a removable lock-file race."""

    meeting_date = _meeting_date(args.meeting_date)
    prefix = args.output_prefix or _default_prefix(meeting_date)
    if not isinstance(prefix, str) or _PREFIX_RE.fullmatch(prefix) is None:
        raise FinalReportInputError("output prefix contains unsafe characters")
    output_dir = args.output_dir
    report_io.ensure_directory(output_dir)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_path = output_dir / f".{prefix}.lock"
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise FinalReportInputError(f"publication lock is unsafe or unavailable: {lock_path}") from exc
    acquired = False
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise FinalReportInputError(f"publication lock is not a private regular file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FinalReportInputError(f"another publication is active for prefix: {prefix}") from exc
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _serialized_publication(function):
    @wraps(function)
    def wrapper(args: argparse.Namespace) -> dict[str, Any]:
        with _publication_lock(args):
            return function(args)

    return wrapper


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register final-report arguments on an existing parser."""

    parser.add_argument("--method-a-report", type=Path, required=True)
    parser.add_argument("--method-a-audit-manifest", type=Path, required=True)
    parser.add_argument("--method-a-name", default="G1.1")
    parser.add_argument("--method-b-report", type=Path, required=True)
    parser.add_argument("--method-b-audit-manifest", type=Path, required=True)
    parser.add_argument("--method-b-name", default="OpenHands")
    parser.add_argument("--dataset", choices=(_DATASET_ID,), default=_DATASET_ID)
    parser.add_argument("--dataset-file", type=Path, required=True)
    parser.add_argument("--meeting-date", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--narrative-json", type=Path)
    parser.add_argument("--labels-json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-prefix")
    parser.add_argument("--latex-engine", default="xelatex")
    parser.add_argument("--latex-timeout", type=float, default=120.0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencollab_eval.commands.swe_final_report",
        description="Build one final comparison JSON, Markdown, TeX, PDF, and manifest.",
    )
    add_arguments(parser)
    return parser


@_serialized_publication
def run_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Validate all evidence, render in a clean directory, and publish atomically."""

    method_a_name = _nonempty(args.method_a_name, label="method A name")
    method_b_name = _nonempty(args.method_b_name, label="method B name")
    if method_a_name == method_b_name:
        raise FinalReportInputError("method names must be distinct")
    for method_name in (method_a_name, method_b_name):
        if _METHOD_RE.fullmatch(method_name) is None:
            raise FinalReportInputError(f"method name is unsafe for the report schema: {method_name}")
    dataset = _nonempty(args.dataset, label="dataset")
    if dataset != _DATASET_ID:
        raise FinalReportInputError(f"dataset must be {_DATASET_ID}")
    author = _nonempty(args.author, label="author")
    meeting_date = _meeting_date(args.meeting_date)
    if not isinstance(args.latex_timeout, (int, float)) or args.latex_timeout <= 0:
        raise FinalReportInputError("LaTeX timeout must be positive")
    prefix = args.output_prefix or _default_prefix(meeting_date)
    if not isinstance(prefix, str) or _PREFIX_RE.fullmatch(prefix) is None:
        raise FinalReportInputError("output prefix contains unsafe characters")
    output_dir = args.output_dir
    report_io.ensure_directory(output_dir)
    manifest_path = output_dir / f"{prefix}.manifest.json"
    _safe_output_target(manifest_path)
    try:
        manifest_path.lstat()
    except FileNotFoundError:
        manifest_existed = False
    else:
        manifest_existed = True
    if not manifest_existed:
        _write_status_manifest(
            manifest_path,
            {
                "schema": "opencollab.swe_final_report_publication.v1",
                "status": "building",
                "meeting_date": meeting_date,
                "output_prefix": prefix,
            },
        )

    expected = tuple(range(1, 101))
    try:
        try:
            dataset_source = load_dataset_census(args.dataset_file, expected=expected)
        except DatasetInputError as exc:
            raise FinalReportInputError(str(exc)) from exc
        method_a = load_method_facts(
            args.method_a_report,
            name=method_a_name,
            expected=expected,
            dataset_tasks=dataset_source.tasks,
        )
        method_b = load_method_facts(
            args.method_b_report,
            name=method_b_name,
            expected=expected,
            dataset_tasks=dataset_source.tasks,
        )
        audit_a = load_audit_manifest(
            args.method_a_audit_manifest,
            method=method_a,
            expected=expected,
            dataset_tasks=dataset_source.tasks,
            expected_dataset_sha256=dataset_source.sha256,
        )
        audit_b = load_audit_manifest(
            args.method_b_audit_manifest,
            method=method_b,
            expected=expected,
            dataset_tasks=dataset_source.tasks,
            expected_dataset_sha256=dataset_source.sha256,
        )
        for runtime_field in ("dataset_sha256", "opencollab_commit", "opencollab_eval_commit"):
            if audit_a["runtime"][runtime_field] != audit_b["runtime"][runtime_field]:
                raise FinalReportInputError(f"method audit manifests use different {runtime_field} identities")
        narrative = load_optional_document(
            args.narrative_json,
            schema=NARRATIVE_SCHEMA,
            label="narrative document",
        )
        labels = load_optional_document(
            args.labels_json,
            schema=LABELS_SCHEMA,
            label="labels document",
        )
        model = build_comparison(
            method_a=method_a,
            method_b=method_b,
            audit_a=audit_a,
            audit_b=audit_b,
            expected=expected,
            dataset=dataset,
            dataset_source=dataset_source,
            author=author,
            meeting_date=meeting_date,
            narrative=narrative,
            labels=labels,
        )
        markdown = render_markdown(model)
        tex = render_tex(model)
        with tempfile.TemporaryDirectory(prefix=f".{prefix}.", dir=output_dir) as raw_stage:
            stage = Path(raw_stage)
            json_path = stage / f"{prefix}.json"
            markdown_path = stage / f"{prefix}.md"
            tex_path = stage / f"{prefix}.tex"
            report_io.write_json(json_path, model)
            report_io.write_text(markdown_path, markdown)
            report_io.write_text(tex_path, tex)
            pdf_path, _latex_log = _compile_pdf(
                tex_path,
                output_dir=stage,
                latex_engine=args.latex_engine,
                timeout_seconds=float(args.latex_timeout),
            )
            staged_outputs = {
                "json": json_path,
                "markdown": markdown_path,
                "tex": tex_path,
                "pdf": pdf_path,
            }
            hashes = {name: _file_sha256(path) for name, path in staged_outputs.items()}
            output_paths = {name: str(output_dir / staged_outputs[name].name) for name in staged_outputs}
            final_manifest = {
                "schema": "opencollab.swe_final_report_publication.v1",
                "status": "final",
                "meeting_date": meeting_date,
                "output_prefix": prefix,
                "inputs": {
                    "dataset": {
                        "name": dataset,
                        "path": str(dataset_source.path),
                        "sha256": dataset_source.sha256,
                    },
                    "method_a": {
                        "name": method_a_name,
                        "fact_report": str(args.method_a_report),
                        "fact_report_sha256": method_a.source.sha256,
                        "audit_manifest": str(args.method_a_audit_manifest),
                        "audit_manifest_sha256": audit_a["manifest_sha256"],
                    },
                    "method_b": {
                        "name": method_b_name,
                        "fact_report": str(args.method_b_report),
                        "fact_report_sha256": method_b.source.sha256,
                        "audit_manifest": str(args.method_b_audit_manifest),
                        "audit_manifest_sha256": audit_b["manifest_sha256"],
                    },
                },
                "outputs": {
                    name: {"path": output_paths[name], "sha256": hashes[name]}
                    for name in ("json", "markdown", "tex", "pdf")
                },
            }
            _publish_transaction(
                stage=stage,
                staged_outputs=staged_outputs,
                hashes=hashes,
                output_dir=output_dir,
                manifest_path=manifest_path,
                final_manifest=final_manifest,
            )
        final_manifest["manifest_path"] = str(manifest_path)
        return final_manifest
    except Exception as exc:
        failure = {
            "schema": "opencollab.swe_final_report_publication.v1",
            "status": "failed",
            "meeting_date": meeting_date,
            "output_prefix": prefix,
            "reason": str(exc),
        }
        if not manifest_existed:
            _write_status_manifest(manifest_path, failure)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = run_from_args(args)
    except (FinalReportInputError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["add_arguments", "main", "run_from_args"]
