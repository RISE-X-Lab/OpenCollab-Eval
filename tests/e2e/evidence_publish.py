"""Publish portable evidence referenced by a deterministic production report."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rewrite_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    for source, replacement in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(source, replacement)
    return value


def publish_production_evidence(
    report: dict[str, Any],
    markdown: str,
    *,
    artifact_dir: Path,
    production_run: Path,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    row = report["rows"][0]
    generation = row["generation"]
    evaluation = row["eval"]
    summary = evaluation["summary"]
    sources = {
        "generation_log": Path(generation["log"]),
        "direct_eval_report": Path(evaluation["report_path"]),
        "direct_eval_command_log": Path(summary["command_log"]),
    }
    evidence_dir = artifact_dir / "production-evidence"
    evidence_dir.mkdir()
    production_root = production_run.resolve()
    replacements: dict[str, str] = {}
    files: dict[str, dict[str, str]] = {}
    for role, source in sources.items():
        resolved = source.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(production_root):
            raise RuntimeError(f"production evidence path escaped its run directory: {role}")
        relative = Path("production-evidence") / f"{role}.txt"
        if role == "direct_eval_report":
            relative = relative.with_suffix(".json")
        destination = artifact_dir / relative
        shutil.copy2(resolved, destination)
        replacements[str(source)] = relative.as_posix()
        replacements[str(resolved)] = relative.as_posix()
        files[role] = {"path": relative.as_posix(), "sha256": _sha256(destination)}
    replacements[str(production_root)] = (
        "disposable-production-run-removed-after-evidence-publication"
    )
    remote_runtime = str(report.get("remote_runtime_repo") or "")
    if remote_runtime:
        replacements[remote_runtime] = (
            "disposable-runtime-removed-after-sha256-verification"
        )
    published_report = _rewrite_paths(report, replacements)
    published_markdown = _rewrite_paths(markdown, replacements)
    disposable_root = str(artifact_dir / "work")
    if disposable_root in json.dumps(published_report) or disposable_root in published_markdown:
        raise RuntimeError("published report still references disposable work paths")
    index = {
        "schema": "opencollab.deterministic_production_evidence.v1",
        "files": files,
    }
    (artifact_dir / "production-evidence-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return published_report, published_markdown, index
