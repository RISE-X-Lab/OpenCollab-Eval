"""Read-only SWE-bench run discovery for thin status scripts."""

from __future__ import annotations

import json as json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_eval_decision import (
    TECHNICAL_EVAL_STATUSES,
    EvalReportSummary,
    TaskSnapshot,
)
from opencollab_eval.engine.swe_eval_records import (
    direct_eval_done_has_execution_proof,
    direct_payload_patch_sha,
    direct_payload_task_id,
    latest_paired_rows,
    patch_sha,
    patch_sha_matches,
    prediction_patch,
    read_bounded_json,
    read_jsonl,
    row_record_id,
    row_task_id,
    strict_integer,
    task_ids,
)
from opencollab_eval.engine.swe_eval_records import (
    row_patch_sha as row_patch_sha,
)


@dataclass(frozen=True)
class EvalReport:
    task_id: str
    patch_sha: str
    status: str
    record_id: str = ""
    resolved_count: int = 0
    unresolved_count: int = 0
    path: str = ""


@dataclass(frozen=True)
class EvalAttempt:
    task_id: str
    record_id: str
    patch_sha: str
    started_at_ns: int
    status: str
    pid: int = 0
    owner_start_identity: str = ""
    evaluator_pgid: int = 0
    evaluator_start_identity: str = ""
    path: str = ""
    prior_reports: dict[str, str] | None = None
    prior_report_fingerprint: str | None = None


REPORT_BINDING_ATTEMPT_STATUSES = {"launching", "started", "completed"}
MAX_DISCOVERY_JSON_FILES = 10_000
MAX_DISCOVERY_ENTRIES = 50_000
MAX_DISCOVERY_DEPTH = 64
MAX_DISCOVERY_JSON_FILE_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_JSON_TOTAL_BYTES = 128 * 1024 * 1024
LEGACY_ATTEMPT_ACTIVE_GRACE_NS = 30 * 1_000_000_000


class EvalArtifactDiscoveryError(RuntimeError):
    """Raised when evaluation artifacts cannot be enumerated completely."""


def _status_from_official_payload(task_id: str, payload: dict[str, Any]) -> tuple[str, int, int, str] | None:
    item = payload.get(task_id)
    if not isinstance(item, dict):
        return None
    item_task_id = direct_payload_task_id(item)
    if item_task_id is None or (item_task_id and item_task_id != task_id):
        return None
    patch_sha = direct_payload_patch_sha(item)
    if patch_sha is None:
        return None
    status = str(item.get("status") or "")
    if status in TECHNICAL_EVAL_STATUSES or bool(item.get("error")):
        return "technical_eval_failed", 0, 0, patch_sha
    if not isinstance(item.get("resolved"), bool):
        return None
    resolved = 1 if item["resolved"] else 0
    unresolved = 0 if item["resolved"] else 1
    return "done", resolved, unresolved, patch_sha


def _summary_count(payload: dict[str, Any], key: str, ids_key: str) -> int:
    value = payload.get(key)
    if value is None and isinstance(payload.get(ids_key), list):
        return len(payload[ids_key])
    if isinstance(value, list):
        return len(value)
    if value is None or value == "":
        return 0
    # JSON booleans are a distinct type from numeric counts.  Python's
    # ``bool`` subclasses ``int``, so accepting them here would let a malformed
    # summary claim one resolved/unresolved task and cross the status boundary
    # as if it contained a real count.
    if isinstance(value, bool):
        raise ValueError(f"invalid {key}: boolean count")
    if isinstance(value, int) and value >= 0:
        return value
    parsed = strict_integer(value, nonnegative=True)
    if parsed is not None:
        return parsed
    raise ValueError(f"invalid {key}: {value!r}")


def _status_from_summary_payload(payload: dict[str, Any]) -> tuple[str, int, int]:
    status = str(payload.get("status") or "")
    if status in TECHNICAL_EVAL_STATUSES or bool(payload.get("error")):
        return "technical_eval_failed", 0, 0
    count_fields = {
        "resolved_instances",
        "unresolved_instances",
        "resolved_ids",
        "unresolved_ids",
    }
    has_count_evidence = any(key in payload for key in count_fields)
    if not has_count_evidence and isinstance(payload.get("resolved"), bool):
        resolved = 1 if payload["resolved"] else 0
        unresolved = 0 if payload["resolved"] else 1
        return status, resolved, unresolved
    if not has_count_evidence:
        raise ValueError("evaluation summary has no resolved or unresolved evidence")
    resolved = _summary_count(payload, "resolved_instances", "resolved_ids")
    unresolved = _summary_count(payload, "unresolved_instances", "unresolved_ids")
    if status == "done" and resolved + unresolved == 0:
        raise ValueError("completed evaluation summary has no task outcome")
    # A per-task direct summary must not expose a verdict that disagrees with
    # its optional count form.  Otherwise a malformed artifact can inflate or
    # invert aggregate results while still carrying ``status=done``.
    verdict = payload.get("resolved")
    if isinstance(verdict, bool) and (
        resolved + unresolved != 1 or (resolved == 1) != verdict
    ):
        raise ValueError("resolved verdict disagrees with outcome counts")
    return status, resolved, unresolved


def _direct_eval_done_has_execution_proof(payload: dict[str, Any]) -> bool:
    # Discovery is the cache/status boundary: a modern done summary must carry
    # the immutable image identity before it can be reused as an evaluation
    # result.  The generic proof helper remains usable by producers that bind
    # the image separately through an explicit expectation.
    return direct_eval_done_has_execution_proof(payload, require_eval_image_id=True)


def _reports_from_payload(path: Path, payload: Any) -> list[EvalReport]:
    if not isinstance(payload, dict):
        return []
    if payload.get("schema") in {
        "opencollab.swe_eval_attempt.v1",
        "opencollab.swe_eval_claim.v1",
    }:
        return []

    # Canonicalize all compatibility aliases before selecting a report.  A
    # conflicting alias must never be resolved by field-order luck (different
    # consumers historically preferred different names).
    task_id = direct_payload_task_id(payload)
    if task_id is None:
        return []
    if task_id:
        if payload.get("schema") != "opencollab.prolite_direct_eval.v2":
            return []
        status_value = str(payload.get("status") or "")
        if (
            status_value not in TECHNICAL_EVAL_STATUSES
            and not bool(payload.get("error"))
            and not _direct_eval_done_has_execution_proof(payload)
        ):
            return []
        try:
            status, resolved, unresolved = _status_from_summary_payload(payload)
        except (TypeError, ValueError):
            return []
        patch_sha = direct_payload_patch_sha(payload)
        if patch_sha is None:
            return []
        return [
            EvalReport(
                task_id=task_id,
                patch_sha=patch_sha,
                status=status,
                record_id=str(payload.get("record_id") or ""),
                resolved_count=resolved,
                unresolved_count=unresolved,
                path=str(path),
            )
        ]

    reports: list[EvalReport] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        schema = str(value.get("schema") or "")
        if schema.startswith("opencollab.prolite_direct_eval."):
            # Nested direct reports are keyed by task for legacy batch files,
            # but the value can also carry one or more identity aliases.  Do
            # not let an outer key relabel a proof generated for another task.
            value_task_id = direct_payload_task_id(value)
            if value_task_id is None:
                continue
            status_value = str(value.get("status") or "")
            technical_value = (
                status_value in TECHNICAL_EVAL_STATUSES or bool(value.get("error"))
            )
            if value_task_id and value_task_id != str(key):
                continue
            # Preserve old technical-failure maps whose inner record omitted
            # an identity; a successful v2 proof must identify itself.
            if not value_task_id and not technical_value:
                continue
            if (
                not technical_value
                and not _direct_eval_done_has_execution_proof(value)
            ):
                continue
            try:
                status, resolved, unresolved = _status_from_summary_payload(value)
            except (TypeError, ValueError):
                continue
            patch_sha = direct_payload_patch_sha(value)
            if patch_sha is None:
                continue
            reports.append(
                EvalReport(
                    task_id=str(key),
                    patch_sha=patch_sha,
                    status=status,
                    record_id=str(
                        value.get("record_id") or payload.get("record_id") or ""
                    ),
                    resolved_count=resolved,
                    unresolved_count=unresolved,
                    path=str(path),
                )
            )
            continue
        official = _status_from_official_payload(str(key), payload)
        if official is None:
            continue
        status, resolved, unresolved, patch_sha = official
        reports.append(
            EvalReport(
                task_id=str(key),
                patch_sha=patch_sha,
                status=status,
                record_id=str(value.get("record_id") or payload.get("record_id") or ""),
                resolved_count=resolved,
                unresolved_count=unresolved,
                path=str(path),
            )
        )
    return reports


def _attempt_from_payload(path: Path, payload: Any) -> EvalAttempt | None:
    if not isinstance(payload, dict) or payload.get("schema") != "opencollab.swe_eval_attempt.v1":
        return None
    task_id_value = direct_payload_task_id(payload)
    if task_id_value is None:
        return None
    task_id = task_id_value
    record_id = str(payload.get("record_id") or "")
    patch_sha = str(payload.get("patch_sha256") or "")
    started_at_ns = strict_integer(payload.get("started_at_ns", 0), nonnegative=True)
    pid = strict_integer(payload.get("pid", 0), nonnegative=True)
    evaluator_pgid = strict_integer(payload.get("evaluator_pgid", 0), nonnegative=True)
    if started_at_ns is None or pid is None or evaluator_pgid is None:
        return None
    if not task_id or not record_id or len(patch_sha) != 64 or started_at_ns <= 0:
        return None
    return EvalAttempt(
        task_id=task_id,
        record_id=record_id,
        patch_sha=patch_sha,
        started_at_ns=started_at_ns,
        status=str(payload.get("status") or ""),
        pid=pid,
        owner_start_identity=str(payload.get("owner_start_identity") or ""),
        evaluator_pgid=evaluator_pgid,
        evaluator_start_identity=str(
            payload.get("evaluator_start_identity") or ""
        ),
        path=str(path),
        prior_reports=(
            {str(key): str(value) for key, value in payload["prior_reports"].items()}
            if isinstance(payload.get("prior_reports"), dict)
            else None
        ),
        prior_report_fingerprint=(
            str(payload.get("prior_report_fingerprint") or "")
            if "prior_report_fingerprint" in payload
            else None
        ),
    )


def _report_path_sort_key(path: Path) -> tuple[int, str]:
    try:
        mtime_ns = path.lstat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return mtime_ns, str(path)


def _report_stat_fingerprint(info: os.stat_result) -> str:
    """Return the same metadata fingerprint captured before an eval attempt."""
    return f"{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}:{info.st_ino}"


def _pid_is_active(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_is_active(pgid: int) -> bool:
    if pgid <= 1:
        return False
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _process_start_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _attempt_is_active(attempt: EvalAttempt) -> bool:
    if attempt.status == "started" and attempt.evaluator_pgid > 1:
        if not attempt.evaluator_start_identity:
            return False
        return bool(
            _process_group_is_active(attempt.evaluator_pgid)
            and _process_start_identity(attempt.evaluator_pgid)
            == attempt.evaluator_start_identity
        )
    if attempt.status in {"launching", "started"} and attempt.pid > 0:
        if attempt.owner_start_identity:
            return bool(
                _pid_is_active(attempt.pid)
                and _process_start_identity(attempt.pid)
                == attempt.owner_start_identity
            )
        age_ns = time.time_ns() - attempt.started_at_ns
        return bool(
            _pid_is_active(attempt.pid)
            and 0 <= age_ns <= LEGACY_ATTEMPT_ACTIVE_GRACE_NS
        )
    return False


def _attempt_has_unverifiable_active_state(attempt: EvalAttempt | None) -> bool:
    if attempt is None or attempt.status not in {"launching", "started"}:
        return False
    if _attempt_is_active(attempt):
        return False
    if attempt.owner_start_identity or attempt.evaluator_start_identity:
        return True
    age_ns = time.time_ns() - attempt.started_at_ns
    return age_ns > LEGACY_ATTEMPT_ACTIVE_GRACE_NS


def _open_discovery_directory(path: Path) -> int:
    try:
        before = path.lstat()
    except OSError as exc:
        raise EvalArtifactDiscoveryError(
            f"cannot inspect evaluation report directory: {path}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode):
        raise EvalArtifactDiscoveryError(
            f"evaluation report directory must be a real directory: {path}"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvalArtifactDiscoveryError(
            f"cannot open evaluation report directory: {path}"
        ) from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(fd)
        raise EvalArtifactDiscoveryError(
            f"evaluation report directory changed while opening: {path}"
        )
    return fd


def _scan_discovery_json_paths(side_dir: Path) -> list[tuple[Path, os.stat_result]]:
    paths: list[tuple[Path, os.stat_result]] = []
    pending: list[tuple[Path, int]] = [(side_dir, 0)]
    scanned_entries = 0
    total_bytes = 0
    while pending:
        directory, depth = pending.pop()
        fd = _open_discovery_directory(directory)
        try:
            try:
                iterator = os.scandir(fd)
            except OSError as exc:
                raise EvalArtifactDiscoveryError(
                    f"cannot scan evaluation report directory: {directory}"
                ) from exc
            with iterator:
                for entry in iterator:
                    scanned_entries += 1
                    if scanned_entries > MAX_DISCOVERY_ENTRIES:
                        raise EvalArtifactDiscoveryError(
                            "evaluation report scan exceeds "
                            f"{MAX_DISCOVERY_ENTRIES} directory entries"
                        )
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise EvalArtifactDiscoveryError(
                            f"cannot inspect evaluation report entry: "
                            f"{directory / entry.name}"
                        ) from exc
                    path = directory / entry.name
                    if stat.S_ISLNK(info.st_mode):
                        raise EvalArtifactDiscoveryError(
                            f"evaluation report tree contains a symlink: {path}"
                        )
                    if stat.S_ISDIR(info.st_mode):
                        child_depth = depth + 1
                        if child_depth > MAX_DISCOVERY_DEPTH:
                            raise EvalArtifactDiscoveryError(
                                "evaluation report scan exceeds depth "
                                f"{MAX_DISCOVERY_DEPTH}: {path}"
                            )
                        pending.append((path, child_depth))
                        continue
                    if not entry.name.endswith(".json"):
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise EvalArtifactDiscoveryError(
                            f"evaluation report is not a regular file: {path}"
                        )
                    if info.st_size > MAX_DISCOVERY_JSON_FILE_BYTES:
                        raise EvalArtifactDiscoveryError(
                            f"evaluation report exceeds file byte limit: {path}"
                        )
                    total_bytes += info.st_size
                    if total_bytes > MAX_DISCOVERY_JSON_TOTAL_BYTES:
                        raise EvalArtifactDiscoveryError(
                            "evaluation report scan exceeds total byte limit "
                            f"{MAX_DISCOVERY_JSON_TOTAL_BYTES}"
                        )
                    paths.append((path, info))
                    if len(paths) > MAX_DISCOVERY_JSON_FILES:
                        raise EvalArtifactDiscoveryError(
                            "evaluation report scan exceeds "
                            f"{MAX_DISCOVERY_JSON_FILES} JSON files"
                        )
        finally:
            os.close(fd)
    return paths


def _discover_eval_artifacts(
    side_dir: Path,
) -> tuple[list[EvalReport], dict[str, EvalAttempt]]:
    try:
        side_stat = side_dir.lstat()
    except FileNotFoundError:
        return [], {}
    except OSError as exc:
        raise EvalArtifactDiscoveryError(
            f"cannot inspect evaluation report root: {side_dir}"
        ) from exc
    if not stat.S_ISDIR(side_stat.st_mode):
        raise EvalArtifactDiscoveryError(
            f"evaluation report root must be a real directory: {side_dir}"
        )
    reports_with_paths: list[tuple[EvalReport, Path, os.stat_result]] = []
    attempts: dict[str, EvalAttempt] = {}
    paths = _scan_discovery_json_paths(side_dir)
    paths.sort(key=lambda item: _report_path_sort_key(item[0]))
    for path, scanned_stat in paths:
        document = read_bounded_json(
            path,
            max_bytes=MAX_DISCOVERY_JSON_FILE_BYTES,
        )
        if document is None:
            raise EvalArtifactDiscoveryError(
                f"evaluation report could not be read completely: {path}"
            )
        payload, opened_stat = document
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            scanned_stat.st_dev,
            scanned_stat.st_ino,
        ):
            raise EvalArtifactDiscoveryError(
                f"evaluation report changed while opening: {path}"
            )
        attempt = _attempt_from_payload(path, payload)
        if attempt is not None:
            previous = attempts.get(attempt.task_id)
            if previous is None or attempt.started_at_ns >= previous.started_at_ns:
                attempts[attempt.task_id] = attempt
            continue
        for report in _reports_from_payload(path, payload):
            reports_with_paths.append((report, path, opened_stat))

    reports: list[EvalReport] = []
    for report, path, report_stat in reports_with_paths:
        attempt = attempts.get(report.task_id)
        # Legacy batch reports may carry the evaluated patch hash but omit
        # ``record_id``.  Once an attempt sidecar exists, leaving that report
        # unbound allows the synthetic sidecar failure (which has the record
        # id) to win the current-candidate summary.  Bind only a matching
        # task+patch report that was created/changed after the attempt; an
        # unchanged pre-attempt report must remain legacy evidence and still
        # be shadowed by the conservative synthetic failure below.
        can_bind_legacy_identity = bool(
            report.patch_sha
            and not report.record_id
            and attempt is not None
            and attempt.status in REPORT_BINDING_ATTEMPT_STATUSES
            and patch_sha_matches(report.patch_sha, attempt.patch_sha)
        )
        if report.patch_sha and not can_bind_legacy_identity:
            reports.append(report)
            continue
        if attempt is None or attempt.status not in REPORT_BINDING_ATTEMPT_STATUSES:
            reports.append(report)
            continue
        report_mtime_ns = report_stat.st_mtime_ns
        fresh_for_attempt = False
        if attempt.prior_reports is not None:
            try:
                relative = str(path.relative_to(side_dir))
            except ValueError:
                relative = str(path)
            prior_fingerprint = attempt.prior_reports.get(relative)
            if (
                prior_fingerprint is not None
                and _report_stat_fingerprint(report_stat) == prior_fingerprint
            ):
                reports.append(report)
                continue
            fresh_for_attempt = prior_fingerprint is None or (
                _report_stat_fingerprint(report_stat) != prior_fingerprint
            )
        elif (
            attempt.prior_report_fingerprint is not None
            and path == Path(attempt.path).with_name("report.json")
        ):
            if (
                attempt.prior_report_fingerprint
                and _report_stat_fingerprint(report_stat)
                == attempt.prior_report_fingerprint
            ):
                reports.append(report)
                continue
            fresh_for_attempt = bool(
                attempt.prior_report_fingerprint
                and _report_stat_fingerprint(report_stat)
                != attempt.prior_report_fingerprint
            )
        elif attempt.prior_reports is None:
            reports.append(report)
            continue
        if report_mtime_ns < attempt.started_at_ns:
            reports.append(report)
            continue
        if can_bind_legacy_identity and not fresh_for_attempt:
            reports.append(report)
            continue
        reports.append(
            EvalReport(
                task_id=report.task_id,
                patch_sha=attempt.patch_sha,
                status=report.status,
                record_id=attempt.record_id,
                resolved_count=report.resolved_count,
                unresolved_count=report.unresolved_count,
                path=report.path,
            )
        )

    reported_identities = {
        (report.task_id, report.record_id, report.patch_sha) for report in reports
    }
    for attempt in attempts.values():
        identity = (attempt.task_id, attempt.record_id, attempt.patch_sha)
        if identity not in reported_identities and (
            attempt.status in TECHNICAL_EVAL_STATUSES
            or attempt.status == "completed"
        ):
            reports.append(
                EvalReport(
                    task_id=attempt.task_id,
                    patch_sha=attempt.patch_sha,
                    status="technical_eval_failed",
                    record_id=attempt.record_id,
                    path=attempt.path,
                )
            )
    return reports, attempts


def discover_eval_reports(side_dir: Path) -> list[EvalReport]:
    reports, _attempts = _discover_eval_artifacts(side_dir)
    return reports


def summarize_eval_reports(
    reports: list[EvalReport],
    *,
    task_id: str,
    current_patch_sha: str,
    current_record_id: str = "",
    active_eval: bool = False,
) -> EvalReportSummary:
    ignored = 0
    latest: EvalReport | None = None
    for report in reports:
        if report.task_id != task_id:
            continue
        if current_record_id and report.record_id and report.record_id != current_record_id:
            ignored += 1
            continue
        if current_patch_sha:
            if not report.patch_sha or not patch_sha_matches(report.patch_sha, current_patch_sha):
                ignored += 1
                continue
        if report.status == "done" or report.status in TECHNICAL_EVAL_STATUSES:
            latest = report

    done = int(latest is not None and latest.status == "done")
    failed = int(latest is not None and latest.status in TECHNICAL_EVAL_STATUSES)
    paths = [latest.path] if latest is not None and latest.path else []
    return EvalReportSummary(
        done_count=done,
        active_count=1 if active_eval else 0,
        failed_count=failed,
        resolved_count=latest.resolved_count if done and latest is not None else 0,
        unresolved_count=latest.unresolved_count if done and latest is not None else 0,
        ignored_patch_mismatch_count=ignored,
        report_paths=tuple(paths),
    )


def build_snapshots(
    run_dir: Path,
    *,
    tasks: list[str] | None = None,
    side_name: str = "official_eval_auto",
    active_generation_tasks: set[str] | None = None,
    active_eval_tasks: set[str] | None = None,
) -> list[TaskSnapshot]:
    predictions = read_jsonl(run_dir / "predictions.jsonl")
    metrics = read_jsonl(run_dir / "metrics.jsonl")
    selected_tasks = tasks or task_ids(predictions, metrics)
    discovery_error: EvalArtifactDiscoveryError | None = None
    try:
        reports, attempts = _discover_eval_artifacts(run_dir / side_name)
    except EvalArtifactDiscoveryError as exc:
        discovery_error = exc
        reports, attempts = [], {}
        if not selected_tasks:
            raise
    active_generation_tasks = active_generation_tasks or set()
    active_eval_tasks = active_eval_tasks or set()
    snapshots: list[TaskSnapshot] = []
    for task_id in selected_tasks:
        pair = latest_paired_rows(predictions, metrics, task_id)
        current_sha = patch_sha(prediction_patch(pair.prediction))
        # Keep the same record-id aliases used by the pairing layer.  Reading
        # only the modern ``record_id`` key here silently drops legacy
        # ``attempt_id``/``workflow_record_id`` identities and lets an
        # unrelated report or active attempt pass the cache boundary.
        current_record_id = row_record_id(pair.prediction)
        current_attempt = attempts.get(task_id)
        attempt_is_active = bool(
            current_attempt is not None
            and current_attempt.status in {"launching", "started"}
            and _attempt_is_active(current_attempt)
            and (
                not current_record_id
                or current_attempt.record_id == current_record_id
            )
            and (
                not current_sha
                or patch_sha_matches(current_attempt.patch_sha, current_sha)
            )
        )
        active_eval = task_id in active_eval_tasks or attempt_is_active
        if discovery_error is None:
            eval_summary = summarize_eval_reports(
                reports,
                task_id=task_id,
                current_patch_sha=current_sha,
                current_record_id=current_record_id,
                active_eval=active_eval,
            )
            if (
                _attempt_has_unverifiable_active_state(current_attempt)
                and not eval_summary.done_count
                and not eval_summary.failed_count
                and not eval_summary.active_count
            ):
                eval_summary = EvalReportSummary(
                    failed_count=1,
                    report_paths=(
                        "attempt_identity_error:"
                        f"{current_attempt.path or current_attempt.task_id}",
                    ),
                )
        else:
            eval_summary = EvalReportSummary(
                active_count=1 if active_eval else 0,
                failed_count=1,
                report_paths=(f"discovery_error:{discovery_error}",),
            )
        snapshots.append(
            TaskSnapshot(
                task_id=task_id,
                active_generation=task_id in active_generation_tasks,
                active_eval=active_eval,
                prediction=pair.prediction,
                metric=pair.metric,
                metric_pairing=pair.status,
                eval_summary=eval_summary,
            )
        )
    return snapshots


def rows_by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row_task_id(row), []).append(row)
    return grouped
