"""Atomic local report publication for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opencollab_eval.commands import _swe_eval_layer_integrity as _integrity
from opencollab_eval.commands import _swe_report_io
from opencollab_eval.safe_files import write_regular_bytes_atomic

_QUARANTINE_SUFFIXES = (".invalid", ".command_failed")
_QUARANTINE_MARKER_BYTES = 512
_SHA256_LENGTH = 64


def _report_candidate_identity(
    row: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    """Extract one unambiguous candidate identity from an eval-only row.

    Queue reconciliation may see reports from several attempts for the same
    task index.  The task, record, and source-patch fields must agree across
    the row's copies; the evaluation-patch hash is derived and may be stale or
    omitted.  Silently choosing conflicting immutable aliases would let a late
    report from another candidate replace the planned verdict.
    """
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    summary = evaluation.get("summary") if isinstance(evaluation.get("summary"), dict) else {}

    def one_text(*values: object) -> str | None:
        present = [str(value) for value in values if value not in (None, "")]
        if not present or len(set(present)) != 1:
            return None
        return present[0]

    task = one_text(row.get("task"), row.get("instance_id"))
    generation_task = one_text(generation.get("task"))
    evaluation_task = one_text(evaluation.get("task"), summary.get("task"))
    if task is None:
        return None
    if generation_task is not None and generation_task != task:
        return None
    if evaluation_task is not None and evaluation_task != task:
        return None

    record_id = one_text(generation.get("record_id"), summary.get("record_id"))
    source_values = (
        generation.get("source_patch_sha256"),
        generation.get("patch_sha256"),
        summary.get("patch_sha256"),
    )
    source_sha = one_text(*source_values)
    eval_sha = one_text(
        generation.get("eval_patch_sha256"),
        summary.get("eval_patch_sha256"),
    )
    # Older eval-only rows carried one generation patch hash only.  In that
    # format the evaluated patch is necessarily the source patch.
    if eval_sha is None and source_sha is not None and not generation.get(
        "eval_patch_sha256"
    ) and not summary.get("eval_patch_sha256"):
        eval_sha = source_sha
    if (
        record_id is None
        or source_sha is None
        or eval_sha is None
        or len(source_sha) != _SHA256_LENGTH
        or len(eval_sha) != _SHA256_LENGTH
        or any(character not in "0123456789abcdefABCDEF" for character in source_sha)
        or any(character not in "0123456789abcdefABCDEF" for character in eval_sha)
    ):
        return None
    return task, record_id, source_sha.lower(), eval_sha.lower()


def _normalized_candidate_identities(
    values: Mapping[object, object] | None,
) -> dict[int, tuple[str, str, str, str]]:
    """Normalize the internal index-to-candidate filter, ignoring bad hints."""
    if not isinstance(values, Mapping) or not values:
        return {}
    normalized: dict[int, tuple[str, str, str, str]] = {}
    for raw_index, raw_identity in values.items():
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            continue
        if not isinstance(raw_identity, (tuple, list)) or len(raw_identity) != 4:
            continue
        task, record_id, source_sha, eval_sha = (str(item) for item in raw_identity)
        if (
            not task
            or not record_id
            or len(source_sha) != _SHA256_LENGTH
            or len(eval_sha) != _SHA256_LENGTH
            or any(character not in "0123456789abcdefABCDEF" for character in source_sha)
            or any(character not in "0123456789abcdefABCDEF" for character in eval_sha)
        ):
            continue
        normalized[raw_index] = (task, record_id, source_sha.lower(), eval_sha.lower())
    return normalized


def _candidate_identity_matches(
    observed: tuple[str, str, str, str],
    expected: tuple[str, str, str, str],
) -> bool:
    """Match the immutable candidate binding, not its derived eval hash.

    ``eval_patch_sha256`` is recomputed from the source patch by eval-only
    runs and can legitimately change when the filtering/runtime code is
    refreshed.  Task, record, and source-patch hashes remain the immutable
    candidate identity that protects reconciliation from another candidate.
    """
    return observed[:3] == expected[:3]


def candidate_row_is_admitted(
    row: dict[str, Any], expected: tuple[str, str, str, str] | None
) -> bool:
    """Admit only an exact identity when this index is queue-bound."""
    if expected is None:
        return True
    observed = _report_candidate_identity(row)
    return observed is not None and _candidate_identity_matches(observed, expected)


def load_candidate_identities_json(value: str | None) -> dict[int, tuple[str, str, str, str]]:
    """Decode and strictly validate the queue's candidate identity map."""
    if not value:
        return {}
    raw, error = _swe_report_io.load_json_with_error(Path(value))
    if error:
        raise ValueError(f"candidate identities are unavailable: {error}")
    if not isinstance(raw, dict):
        raise ValueError("candidate identities must be a JSON object")
    converted: dict[int, object] = {}
    for raw_index, identity in raw.items():
        if not isinstance(raw_index, str) or not raw_index.isdecimal():
            raise ValueError("candidate identity index must be an integer")
        converted[int(raw_index)] = identity
    normalized = _normalized_candidate_identities(converted)
    if len(normalized) != len(converted):
        raise ValueError("candidate identities contain an invalid identity")
    return normalized


def write_candidate_identities_file(
    parent: Path,
    values: Mapping[object, object],
) -> Path:
    """Persist a validated queue identity map in a hidden report-side file."""
    normalized = _normalized_candidate_identities(values)
    if len(normalized) != len(values):
        raise ValueError("candidate identities contain an invalid identity")
    path = parent / f".candidate-identities-{uuid.uuid4().hex}.json"
    _swe_report_io.write_json(
        path,
        {str(index): list(identity) for index, identity in normalized.items()},
    )
    return path


def remove_candidate_identities_file(path: Path | None) -> None:
    """Best-effort cleanup that never masks the report result."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def candidate_eval_attempt_counts(
    report: dict[str, Any],
    *,
    expected_index: int | None = None,
    expected_candidate: tuple[str, str, str, str] | None = None,
) -> dict[int, int]:
    """Count attempts while excluding stale rows for a bound candidate."""
    counts: dict[int, int] = {}
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    for result in report.get("results") or []:
        if isinstance(result, dict):
            rows.extend(row for row in result.get("rows") or [] if isinstance(row, dict))
    for row in rows:
        index = _integrity.strict_index(row.get("index"))
        if index is None:
            continue
        if (
            expected_candidate is not None
            and index == expected_index
            and not candidate_row_is_admitted(row, expected_candidate)
        ):
            continue
        evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
        if evaluation.get("executed") is False:
            attempt_count = 0
        else:
            attempt_count = _integrity.strict_nonnegative_integer(
                evaluation.get("attempt_count", 0)
            )
            attempt_count = attempt_count if attempt_count is not None else 0
        counts[index] = counts.get(index, 0) + attempt_count
    return counts


def final_candidate_eval_attempt_counts(
    report: dict[str, Any],
    *,
    expected_index: int | None = None,
    expected_candidate: tuple[str, str, str, str] | None = None,
) -> dict[int, int]:
    """Count final-report attempts only for the bound candidate."""
    counts: dict[int, int] = {}
    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        index = _integrity.strict_index(task.get("index"))
        if expected_candidate is not None and index == expected_index:
            observed = tuple(
                str(task.get(field) or "")
                for field in ("task", "record_id", "patch_sha256", "eval_patch_sha256")
            )
            if not _candidate_identity_matches(observed, expected_candidate):
                continue
        raw_count = task.get("observed_eval_attempt_count")
        if raw_count is None:
            raw_count = task.get("eval_attempt_count", 0)
        count = _integrity.strict_nonnegative_integer(raw_count)
        if index is not None and count is not None:
            counts[index] = max(counts.get(index, 0), count)
    return counts


def _has_reconciliation_verdict(row: dict[str, Any]) -> bool:
    """Return whether a row carries a terminal eval verdict, not just evidence."""
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    summary = evaluation.get("summary")
    return bool(
        evaluation.get("status") == "eval_done"
        and isinstance(summary, dict)
        and isinstance(summary.get("resolved"), bool)
    )


def _report_projection_identity(
    row: dict[str, Any],
    observed_identity: tuple[str, str, str, str] | None,
) -> tuple[str, tuple[str, ...]] | None:
    """Extract the eval projection key used by the final-report integrity gate."""
    if observed_identity is None:
        return None
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    summary = evaluation.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    generation = row.get("generation")
    generation = generation if isinstance(generation, dict) else {}
    path_values: list[tuple[str, ...]] = []
    for container in (generation, summary):
        if "filtered_patch_paths" not in container:
            continue
        value = container.get("filtered_patch_paths")
        if (
            not isinstance(value, list)
            or any(not isinstance(path, str) or not path for path in value)
            or len(value) != len(set(value))
        ):
            return None
        path_values.append(tuple(value))
    if not path_values or len(set(path_values)) != 1:
        return None
    return observed_identity[3], path_values[0]


def _regular_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _metadata_marker_identity(path: Path) -> tuple[int, int] | None:
    try:
        value = json.loads(
            _swe_report_io.read_text(path, max_bytes=_QUARANTINE_MARKER_BYTES)
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    identity = value.get("source_identity") if isinstance(value, dict) else None
    if (
        not isinstance(identity, list)
        or len(identity) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in identity)
    ):
        return None
    return identity[0], identity[1]


def _has_quarantine_marker(path: Path) -> bool:
    """Return whether a same-identity queue quarantine marker remains."""
    identity = _regular_identity(path)
    if identity is None:
        return False
    for suffix in _QUARANTINE_SUFFIXES:
        for marker in path.parent.glob(path.name + suffix + "*"):
            if _regular_identity(marker) == identity or _metadata_marker_identity(marker) == identity:
                return True
    return False


def _create_marker(path: Path, payload: bytes) -> bool:
    try:
        fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        return False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("quarantine marker write made no progress")
            view = view[written:]
        os.fsync(fd)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)
    return True


def quarantine_report(
    path: Path, suffix: str, *, replace_first: bool = False
) -> Path | None:
    """Quarantine a report while retaining a marker if source retirement races."""
    if suffix not in _QUARANTINE_SUFFIXES:
        raise ValueError(f"unsupported quarantine suffix: {suffix}")
    identity = _regular_identity(path)
    if identity is None:
        return None
    if replace_first:
        target = path.with_name(path.name + suffix)
        try:
            path.replace(target)
            return target
        except OSError:
            pass
    for ordinal in range(100):
        tag = suffix if ordinal == 0 else f"{suffix}.{ordinal}"
        target = path.with_name(path.name + tag)
        try:
            os.link(path, target, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError:
            break
        current = _regular_identity(path)
        linked = _regular_identity(target)
        if current != identity or linked != identity:
            try:
                target.unlink()
            except OSError:
                pass
            continue
        try:
            path.unlink()
        except OSError:
            # Keep the same-identity hard link as a durable exclusion marker.
            return target
        return target
    marker_payload = json.dumps({"source_identity": list(identity)}).encode("ascii")
    for ordinal in range(100):
        tag = suffix if ordinal == 0 else f"{suffix}.{ordinal}"
        target = path.with_name(path.name + tag)
        try:
            if not _create_marker(target, marker_payload):
                continue
        except FileExistsError:
            continue
        return target
    return None


def _local_report_target_expectation(path: Path) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"report destination must be regular or absent: {path}")
    if before is None:
        return {"require_target_absent": True}
    return {"expected_target_identity": (before.st_dev, before.st_ino)}


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    if os.path.abspath(json_path) == os.path.abspath(md_path):
        raise ValueError("JSON and Markdown reports must use different paths")
    bundle_id = uuid.uuid4().hex
    bundled_summary = {**summary, "local_report_bundle_id": bundle_id}
    json_payload = (json.dumps(bundled_summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    markdown = markdown.rstrip("\n") + f"\n\n<!-- local_report_bundle_id:{bundle_id} -->\n"
    json_expectation = _local_report_target_expectation(json_path)
    md_expectation = _local_report_target_expectation(md_path)
    # JSON is the bundle commit marker. A reader accepts the pair only when its
    # bundle id matches the Markdown marker, so publish Markdown first.
    write_regular_bytes_atomic(
        md_path,
        markdown.encode("utf-8"),
        **md_expectation,
    )
    write_regular_bytes_atomic(json_path, json_payload, **json_expectation)


def eval_only_reconciliation_reports(
    parent_output_dir: Path,
    current_report: Path,
    *,
    ignored_paths: tuple[Path, ...] | list[Path] = (),
    candidate_identities: Mapping[object, object] | None = None,
) -> list[Path]:
    """Select execution evidence and the newest verdict per task.

    When ``candidate_identities`` is supplied by the queue, reports for a
    known index are admitted only when they carry that exact candidate
    identity.  This keeps a late report from an older candidate from winning
    the mtime race while preserving same-candidate historical attempts.
    """
    current = current_report.absolute()
    ignored = {Path(path).absolute() for path in ignored_paths}
    expected_identities = _normalized_candidate_identities(candidate_identities)
    candidates = set(parent_output_dir.glob("task_*_eval_only_*.json"))
    final_report_path = parent_output_dir / "final_eval_layer_report.json"
    try:
        final_report_path.lstat()
    except FileNotFoundError:
        pass
    else:
        final_report, load_error = _swe_report_io.load_json_with_error(
            final_report_path
        )
        if load_error:
            raise RuntimeError(
                f"prior final report is unavailable: {load_error}: {final_report_path}"
            )
        source_reports = final_report.get("source_reports")
        if not isinstance(source_reports, list) or not source_reports or any(
            not isinstance(value, str) or not value or not Path(value).is_absolute()
            for value in source_reports
        ):
            raise RuntimeError(
                f"prior final report has invalid source_reports: {final_report_path}"
            )
        parent_summary = (parent_output_dir / "parallel_summary.json").absolute()
        for value in source_reports:
            historical_path = Path(value).absolute()
            if historical_path != parent_summary:
                candidates.add(historical_path)
    candidates.add(current)
    selected_execution: set[Path] = set()
    selected_verdict: dict[
        int,
        tuple[
            tuple[int, str],
            Path,
            tuple[str, str, str, str] | None,
            tuple[str, tuple[str, ...]] | None,
        ],
    ] = {}
    selected_any: dict[
        int,
        tuple[
            tuple[int, str],
            Path,
            tuple[str, str, str, str] | None,
            tuple[str, tuple[str, ...]] | None,
        ],
    ] = {}
    accepted: list[
        tuple[
            Path,
            int,
            tuple[str, str, str, str] | None,
            tuple[str, tuple[str, ...]] | None,
            bool,
        ]
    ] = []
    for path in candidates:
        path = path.absolute()
        if path != current and (path in ignored or _has_quarantine_marker(path)):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"eval-only report must be a regular file: {path}")
        report = _swe_report_io.load_json(path)
        rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
        indices = {_integrity.strict_index(row.get("index")) for row in rows}
        indices.discard(None)
        if len(rows) != 1 or len(indices) != 1:
            raise RuntimeError(f"eval-only report must contain one indexed row: {path}")
        index = next(iter(indices))
        expected_identity = expected_identities.get(index)
        observed_identity = _report_candidate_identity(rows[0])
        if expected_identity is not None and (
            observed_identity is None
            or not _candidate_identity_matches(observed_identity, expected_identity)
        ):
            continue
        verdict_score = (info.st_mtime_ns, str(path))
        projection_identity = _report_projection_identity(rows[0], observed_identity)
        entry = (verdict_score, path, observed_identity, projection_identity)
        if index not in selected_any or verdict_score > selected_any[index][0]:
            selected_any[index] = entry
        # Under an explicit queue binding, a late execution-only artifact must
        # not displace a real verdict merely because its mtime is newer.  Keep
        # the historical mtime rule among rows that actually carry a verdict.
        if (
            expected_identity is None
            or _has_reconciliation_verdict(rows[0])
        ) and (
            index not in selected_verdict or verdict_score > selected_verdict[index][0]
        ):
            selected_verdict[index] = entry
        accepted.append(
            (
                path,
                index,
                observed_identity,
                projection_identity,
                bool(_integrity.eval_attempt_count(rows[0])),
            )
        )
    canonical = {
        index: selected_verdict.get(index) or entry
        for index, entry in selected_any.items()
    }
    if expected_identities:
        # A recomputed eval projection is still the same source candidate, but
        # mixing projections in one final report triggers its integrity gate.
        # Bind execution history to the actual selected verdict projection.
        for path, index, observed_identity, projection_identity, executed in accepted:
            if not executed:
                continue
            if expected_identities.get(index) is None:
                selected_execution.add(path)
                continue
            selected = selected_verdict.get(index) or selected_any.get(index)
            selected_identity = selected[2] if selected else None
            selected_projection = selected[3] if selected else None
            if (
                observed_identity is None
                or selected_identity is None
                or observed_identity != selected_identity
                or selected_projection is None
                and projection_identity is not None
                or selected_projection is not None
                and projection_identity != selected_projection
            ):
                continue
            selected_execution.add(path)
    else:
        selected_execution = {
            path for path, _index, _identity, _projection, executed in accepted if executed
        }
    selected_paths = selected_execution | {entry[1] for entry in canonical.values()}
    return sorted(selected_paths, key=str)


__all__ = [name for name in globals() if not name.startswith("__")]
