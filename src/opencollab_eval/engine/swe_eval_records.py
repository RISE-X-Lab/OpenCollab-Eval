"""Small, pure helpers for SWE-bench prediction/metric records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from opencollab_eval.engine.eval_candidate_projection import (
    candidate_projection_failure_valid,
    candidate_projection_valid,
    candidate_rejection_is_conclusive,
)
from opencollab_eval.engine.swe_eval_record_identity import (
    direct_payload_alias_value,
    direct_payload_patch_sha,  # noqa: F401
    direct_payload_task_id,  # noqa: F401
    strict_integer,  # noqa: F401
)
from opencollab_eval.engine.swe_test_evidence import target_evidence_passed
from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024
MAX_JSONL_RETAINED_BYTES = 128 * 1024 * 1024
MAX_JSONL_RETAINED_ROWS = 10_000
MAX_JSONL_SCAN_BYTES = 256 * 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024
SUBMISSION_INTEGRITY_PROVEN = "proven"
SUBMISSION_INTEGRITY_LEGACY = "legacy_missing_fields"
SUBMISSION_INTEGRITY_INELIGIBLE = "explicitly_ineligible"
SUBMISSION_INTEGRITY_MISSING = "missing_metric"
_REQUIRED_TRUE_SUBMISSION_FIELDS = (
    "submission_eligible",
    "execution_quiesced",
    "patch_extraction_succeeded",
    "injected_path_cleanup_proven",
    "harness_artifact_exclusion_proven",
    "checkpoint_restore_integrity_proven",
    "task_stage_integrity_proven",
)
_REQUIRED_FALSE_SUBMISSION_FIELDS = ("test_patch_isolation_failed",)
_OPTIONAL_TRUE_SUBMISSION_FIELDS = (
    "worktree_integrity_proven",
    "patch_produced",
)


class UnsafeRecordInputError(OSError):
    """Raised when a harness record path cannot be opened as a regular file."""


class RecordInputLimitError(ValueError):
    """Raised when returning rows would require silently truncating input."""


class RecordInputFormatError(ValueError):
    """Raised when a physical JSONL record cannot be decoded completely."""

@contextmanager
def open_regular_binary(path: Path) -> Iterator[BinaryIO]:
    """Open one regular file without following its final symlink component."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow

    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise UnsafeRecordInputError(f"cannot inspect record input {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise UnsafeRecordInputError(f"record input is not a regular file: {path}")

    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise UnsafeRecordInputError(f"cannot safely open record input {path}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeRecordInputError(f"record input is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise UnsafeRecordInputError(f"record input changed while opening: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            yield handle
    finally:
        if fd >= 0:
            os.close(fd)

def read_bounded_json(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[Any, os.stat_result] | None:
    """Read a bounded JSON document and return the opened file's metadata."""
    if max_bytes is None:
        max_bytes = MAX_JSON_DOCUMENT_BYTES
    try:
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > max_bytes:
                return None
            raw = handle.read(max_bytes + 1)
            after_read = os.fstat(handle.fileno())
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    if (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ) != (
        after_read.st_dev,
        after_read.st_ino,
        after_read.st_size,
        after_read.st_mtime_ns,
        after_read.st_ctime_ns,
    ):
        return None
    try:
        return json.loads(raw.decode("utf-8")), after_read
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None

@dataclass(frozen=True)
class PairedRows:
    prediction: dict[str, Any] | None
    metric: dict[str, Any] | None
    status: str

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: deque[tuple[int, dict[str, Any]]] = deque()
    retained_bytes = 0
    try:
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > MAX_JSONL_SCAN_BYTES:
                raise RecordInputLimitError(
                    f"JSONL input exceeds {MAX_JSONL_SCAN_BYTES} bytes: {path}"
                )
            remaining = opened.st_size
            while remaining > 0:
                line = handle.readline(min(MAX_JSONL_LINE_BYTES + 1, remaining))
                if not line:
                    break
                remaining -= len(line)
                if len(line) > MAX_JSONL_LINE_BYTES:
                    raise RecordInputLimitError(
                        f"JSONL line exceeds {MAX_JSONL_LINE_BYTES} bytes: {path}"
                    )
                if not line.strip():
                    continue
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RecordInputFormatError(
                        f"invalid JSONL record in {path}"
                    ) from exc
                if not isinstance(value, dict):
                    raise RecordInputFormatError(
                        f"JSONL record must be an object: {path}"
                    )
                line_size = len(line)
                rows.append((line_size, value))
                retained_bytes += line_size
                if (
                    len(rows) > MAX_JSONL_RETAINED_ROWS
                    or retained_bytes > MAX_JSONL_RETAINED_BYTES
                ):
                    raise RecordInputLimitError(
                        "JSONL input exceeds retained row or byte limit: "
                        f"{path}"
                    )
            after_read = os.fstat(handle.fileno())
            if (
                after_read.st_dev != opened.st_dev
                or after_read.st_ino != opened.st_ino
                or after_read.st_size != opened.st_size
                or after_read.st_mtime_ns != opened.st_mtime_ns
                or after_read.st_ctime_ns != opened.st_ctime_ns
            ):
                raise UnsafeRecordInputError(
                    f"JSONL input changed while reading: {path}"
                )
    except FileNotFoundError:
        return []
    return [value for _size, value in rows]

def prediction_patch(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("model_patch") or row.get("patch") or "")

def row_task_id(row: dict[str, Any] | None) -> str:
    return direct_payload_alias_value(row, ("instance_id", "task_id", "id")) or ""


def row_record_id(row: dict[str, Any] | None) -> str:
    return direct_payload_alias_value(
        row, ("record_id", "attempt_id", "workflow_record_id")
    ) or ""


def row_explicit_patch_sha(row: dict[str, Any] | None) -> str:
    return direct_payload_alias_value(
        row, ("patch_sha256", "patch_sha", "model_patch_sha256")
    ) or ""
def patch_sha(patch: str) -> str:
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()

def row_patch_sha(row: dict[str, Any] | None) -> str:
    patch = prediction_patch(row)
    if patch:
        return patch_sha(patch)
    explicit = row_explicit_patch_sha(row)
    if explicit:
        return explicit
    return ""

def embedded_workflow_metric(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    metric = row.get("workflow_metric")
    if not isinstance(metric, dict):
        return None
    if row_task_id(metric) != row_task_id(row):
        return None
    if row_record_id(metric) != row_record_id(row):
        return None
    prediction_sha = row_patch_sha(row)
    metric_sha = row_patch_sha(metric)
    if not prediction_sha or not metric_sha or not patch_sha_matches(prediction_sha, metric_sha):
        return None
    return metric

def metric_submission_integrity(metric: dict[str, Any] | None) -> str:
    """Classify explicit submission-integrity fields while preserving old rows."""
    if not isinstance(metric, dict):
        return SUBMISSION_INTEGRITY_MISSING

    known_fields = {
        *_REQUIRED_TRUE_SUBMISSION_FIELDS,
        *_REQUIRED_FALSE_SUBMISSION_FIELDS,
        *_OPTIONAL_TRUE_SUBMISSION_FIELDS,
        "checkpoint_result",
    }
    integrity_prefixes = (
        "submission_",
        "execution_quies",
        "patch_extraction",
        "injected_path_cleanup",
        "harness_artifact",
        "checkpoint_restore",
        "task_stage",
        "test_patch_isolation",
        "worktree_integrity",
        "patch_produced",
    )
    has_modern_integrity_signal = bool(known_fields.intersection(metric)) or any(
        isinstance(field, str) and field.startswith(integrity_prefixes)
        for field in metric
    )
    missing_required = False
    for field in _REQUIRED_TRUE_SUBMISSION_FIELDS:
        if field not in metric:
            missing_required = True
        elif metric[field] is not True:
            return SUBMISSION_INTEGRITY_INELIGIBLE
    for field in _REQUIRED_FALSE_SUBMISSION_FIELDS:
        if field not in metric:
            missing_required = True
        elif metric[field] is not False:
            return SUBMISSION_INTEGRITY_INELIGIBLE
    for field in _OPTIONAL_TRUE_SUBMISSION_FIELDS:
        if field in metric and metric[field] is not True:
            return SUBMISSION_INTEGRITY_INELIGIBLE

    checkpoint_result = metric.get("checkpoint_result")
    if (
        isinstance(checkpoint_result, dict)
        and "worktree_integrity_proven" in checkpoint_result
        and checkpoint_result["worktree_integrity_proven"] is not True
    ):
        return SUBMISSION_INTEGRITY_INELIGIBLE
    if isinstance(checkpoint_result, dict):
        restore_result = checkpoint_result.get("restore")
        if isinstance(restore_result, dict):
            if (
                "worktree_integrity_proven" in restore_result
                and restore_result["worktree_integrity_proven"] is not True
            ):
                return SUBMISSION_INTEGRITY_INELIGIBLE
            if (
                restore_result.get("status") == "restored"
                and "submission_eligible" in restore_result
                and restore_result["submission_eligible"] is not True
            ):
                return SUBMISSION_INTEGRITY_INELIGIBLE
        abort_result = checkpoint_result.get("abort")
        if (
            isinstance(abort_result, dict)
            and abort_result.get("status") == "checkpoint_abort_timed_out"
        ):
            return SUBMISSION_INTEGRITY_INELIGIBLE
        final_result = checkpoint_result.get("final")
        if (
            isinstance(final_result, dict)
            and final_result.get("status") == "checkpoint_abort_timed_out"
        ):
            return SUBMISSION_INTEGRITY_INELIGIBLE
    if missing_required:
        if has_modern_integrity_signal:
            return SUBMISSION_INTEGRITY_INELIGIBLE
        return SUBMISSION_INTEGRITY_LEGACY
    return SUBMISSION_INTEGRITY_PROVEN


def is_completed_prediction(row: dict[str, Any] | None) -> bool:
    """Return whether a prediction is safe for a resume/skip decision."""
    if not isinstance(row, dict):
        return False
    patch = prediction_patch(row)
    if not patch.strip() or not row_record_id(row):
        return False
    computed_sha = patch_sha(patch)
    if not patch_sha_matches(row_explicit_patch_sha(row), computed_sha):
        return False
    metric = embedded_workflow_metric(row)
    if metric is None:
        return False
    if metric_submission_integrity(metric) != SUBMISSION_INTEGRITY_PROVEN:
        return False
    status = metric_status(metric)
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool):
        return False
    if status == "done":
        return returncode == 0
    if status == "done_with_timeout_patch":
        return returncode == 124
    return False


def patch_sha_matches(left: str | None, right: str | None) -> bool:
    left_value = str(left or "")
    right_value = str(right or "")
    if not left_value or not right_value:
        return False
    return (
        _SHA256_RE.fullmatch(left_value) is not None
        and _SHA256_RE.fullmatch(right_value) is not None
        # Hexadecimal digests are case-insensitive.  Keep the strict
        # 64-character validation above while accepting legacy reports that
        # serialized an otherwise identical digest with upper-case letters.
        and left_value.lower() == right_value.lower()
    )


def _direct_eval_plan_status(
    tests_status: dict[str, Any],
    prefix: str,
    expected_plan: dict[str, Any] | None,
    *,
    require_commands: bool,
) -> int | None:
    plan = tests_status.get(f"{prefix}_plan")
    evidence = tests_status.get(f"{prefix}_evidence")
    if not isinstance(plan, dict) or not isinstance(evidence, list):
        return None
    if validated_test_plan_kind(plan, require_commands=require_commands) is None:
        return None
    if expected_plan is not None and plan != expected_plan:
        return None
    commands = plan.get("commands")
    if not isinstance(commands, list) or len(evidence) != len(commands):
        return None
    if require_commands and not commands:
        return None
    if commands and plan.get("coverage_verified") is not True:
        return None
    statuses: list[int] = []
    outcomes: list[bool] = []
    unknown_batches: list[int] = []
    for index, item in enumerate(evidence, 1):
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        if not all(
            item.get(field) is True
            for field in (
                "command_matches_plan",
                "log_artifact_safe",
                "artifact_safe",
            )
        ):
            unknown_batches.append(index)
            statuses.append(status)
            continue
        outcome = target_evidence_passed(item)
        if outcome is None:
            unknown_batches.append(index)
            statuses.append(status)
            continue
        statuses.append(status)
        outcomes.append(outcome)
    reported_status = tests_status.get(f"{prefix}_status")
    if isinstance(reported_status, bool) or not isinstance(reported_status, int):
        return None
    if unknown_batches:
        if not outcomes or all(outcomes):
            return None
        return 1
    expected_status = next((status for status in statuses if status != 0), 0)
    if reported_status != expected_status:
        return None
    return 0 if all(outcomes) else 1


def _runtime_file_dependency_proof(
    payload: dict[str, Any],
    tests_status: dict[str, Any],
    expected_f2p_plan: dict[str, Any] | None,
    expected_p2p_plan: dict[str, Any] | None,
) -> bool:
    plans = (
        expected_f2p_plan or tests_status.get("fail_to_pass_plan"),
        expected_p2p_plan or tests_status.get("pass_to_pass_plan"),
    )
    specs: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        for item in plan.get("runtime_dependencies") or []:
            if isinstance(item, dict) and item not in specs:
                specs.append(item)
    file_roots = {
        item.get("root") for item in specs if item.get("kind") == "file"
    }
    if not file_roots:
        return True
    identities = payload.get("runtime_dependency_identities")
    runtime = payload.get("runtime_dependencies")
    identity_entries = identities.get("entries") if isinstance(identities, dict) else None
    runtime_entries = runtime.get("entries") if isinstance(runtime, dict) else None
    image_id = str(payload.get("eval_image_id") or "")
    if (
        not isinstance(identities, dict)
        or identities.get("schema") != "opencollab.runtime_dependency_identities.v1"
        or identities.get("image_id") != image_id
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or not isinstance(identity_entries, list)
        or len(identity_entries) > 16
        or not isinstance(runtime, dict)
        or runtime.get("schema") != "opencollab.eval_runtime_dependencies.v1"
        or runtime.get("phase") != "restored"
        or runtime.get("source") != "pinned_image_runtime_with_trusted_public_preparation"
        or runtime.get("solver_visible") is not False
        or not isinstance(runtime_entries, list)
        or len(runtime_entries) > 16
        or runtime.get("spec_sha256")
        != hashlib.sha256(
            json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    ):
        return False
    identity_hashes = {
        item.get("root"): item.get("content_sha256")
        for item in identity_entries
        if isinstance(item, dict)
        and set(item) == {"root", "content_sha256"}
        and item.get("root") in file_roots
        and _SHA256_RE.fullmatch(str(item.get("content_sha256") or ""))
    }
    if len(identity_hashes) != len(identity_entries):
        return False
    runtime_files = {
        item.get("root"): item.get("content_sha256")
        for item in runtime_entries
        if isinstance(item, dict)
        and item.get("kind") == "file"
        and item.get("candidate_protected") is False
        and item.get("root") in file_roots
        and item.get("required_paths") == [item.get("root")]
    }
    return len(runtime_files) == len(
        [item for item in runtime_entries if isinstance(item, dict) and item.get("kind") == "file"]
    ) and runtime_files == identity_hashes


def direct_eval_done_has_execution_proof(
    payload: dict[str, Any],
    *,
    expected_eval_spec_sha256: str = "",
    expected_f2p_plan: dict[str, Any] | None = None,
    expected_p2p_plan: dict[str, Any] | None = None,
    require_eval_image_id: bool = False,
) -> bool:
    """Validate the common direct-evaluation execution proof."""
    eval_spec_sha256 = str(payload.get("eval_spec_sha256") or "")
    eval_patch_sha256 = str(payload.get("eval_patch_sha256") or "")
    if (
        payload.get("schema") != "opencollab.prolite_direct_eval.v2"
        or payload.get("status") != "done"
        or not isinstance(payload.get("resolved"), bool)
        or payload.get("technical_reasons") != []
        or payload.get("output_artifact_errors") != []
        or isinstance(payload.get("docker_exit"), bool)
        or payload.get("docker_exit") != 0
        or payload.get("cleanup_quiesced") is not True
        or not isinstance(payload.get("container_cleanup"), dict)
        or _SHA256_RE.fullmatch(eval_spec_sha256) is None
        or _SHA256_RE.fullmatch(eval_patch_sha256) is None
        or expected_eval_spec_sha256
        and eval_spec_sha256 != expected_eval_spec_sha256
    ):
        return False
    if require_eval_image_id and re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(payload.get("eval_image_id") or "")
    ) is None:
        return False
    warnings = payload.get("operational_warnings", [])
    if (
        not isinstance(warnings, list)
        or any(not isinstance(item, str) or len(item) > 512 for item in warnings)
        or payload["container_cleanup"].get("ok") is not True
        and "container_removal_failed_after_stop" not in warnings
    ):
        return False
    expectation = payload.get("candidate_expectation")
    projection = payload.get("candidate_projection")
    projection_failure = payload.get("candidate_projection_failure")
    task = direct_payload_task_id(payload)
    public_patch_sha = direct_payload_patch_sha(payload)
    tests_status = payload.get("tests_status")
    if (
        task is None
        or public_patch_sha is None
        or not isinstance(expectation, dict)
        or not isinstance(tests_status, dict)
    ):
        return False
    # Every task alias must identify the same candidate as the immutable
    # expectation.  This also prevents the historical ``task``-first lookup
    # from disagreeing with a consumer that prefers ``instance_id``.
    if task != str(expectation.get("instance_id") or ""):
        return False

    # ``patch_sha256`` is the identity exposed to discovery/status consumers.
    # When present, it must describe the same source patch bound by the
    # expectation; otherwise a valid execution proof can be relabelled as a
    # different candidate after the evaluator has finished.  Keep omission
    # compatible with older producer fixtures, while rejecting every explicit
    # alias that disagrees with the immutable expectation.
    expected_source_sha = str(expectation.get("source_patch_sha256") or "")
    expected_eval_sha = str(expectation.get("eval_patch_sha256") or "")
    if public_patch_sha and public_patch_sha != expected_source_sha:
        return False
    # ``candidate_projection`` and its source projection repeat the source
    # patch identity.  Keep the public alias bound to those copies as well;
    # otherwise a future consumer that trusts one projection field could
    # reintroduce the same relabelling bug.
    for projection_value in (
        projection,
        payload.get("source_candidate_projection"),
        projection_failure,
    ):
        if (
            public_patch_sha
            and isinstance(projection_value, dict)
            and projection_value.get("source_patch_sha256") not in (None, "")
            and projection_value.get("source_patch_sha256") != public_patch_sha
        ):
            return False
    for field, expected_sha in (
        ("source_patch_sha256", expected_source_sha),
        ("eval_patch_sha256", expected_eval_sha),
    ):
        if field in payload and payload.get(field) not in (None, ""):
            if str(payload.get(field)) != expected_sha:
                return False
    base_snapshot = payload.get("base_snapshot_integrity")
    preparation = (
        base_snapshot.get("preparation_input_snapshot")
        if isinstance(base_snapshot, dict)
        else None
    )
    source_failure = (
        isinstance(projection_failure, dict)
        and projection_failure.get("phase") == "source"
    )
    bound_base = preparation if source_failure else base_snapshot
    candidate_rejected = bool(
        isinstance(projection_failure, dict)
        and candidate_rejection_is_conclusive(projection_failure)
        and candidate_projection_failure_valid(
            projection_failure,
            expectation,
            source_projection=payload.get("source_candidate_projection"),
            base_commit=str(
                bound_base.get(
                    "expected_base_commit" if source_failure else "anonymous_head"
                )
                or ""
                if isinstance(bound_base, dict)
                else ""
            ),
            base_tree=str(
                bound_base.get("base_tree") or ""
                if isinstance(bound_base, dict)
                else ""
            ),
        )
        and projection_failure.get("instance_id") == task
        and projection_failure.get("record_id") == payload.get("record_id")
        and projection_failure.get("eval_patch_sha256") == eval_patch_sha256
    )
    if candidate_rejected:
        required_zero = (
            "base_commit_status",
            "before_repo_status",
            "post_before_base_status",
        )
        model_status = tests_status.get("model_patch_status")
        return bool(
            payload["resolved"] is False
            and payload.get("outcome", "unresolved") == "unresolved"
            and payload.get(
                "outcome_basis",
                ["candidate_patch_could_not_be_applied"],
            )
            == ["candidate_patch_could_not_be_applied"]
            and not projection
            and all(tests_status.get(field) == 0 for field in required_zero)
            and isinstance(model_status, int)
            and not isinstance(model_status, bool)
            and model_status != 0
        )
    if (
        not isinstance(projection, dict)
        or projection.get("instance_id") != task
        or projection.get("record_id") != payload.get("record_id")
        or projection.get("eval_patch_sha256") != eval_patch_sha256
        or not candidate_projection_valid(
            projection,
            expectation,
            payload.get("source_candidate_projection"),
        )
    ):
        return False
    for field in (
        "base_commit_status",
        "service_bootstrap_status",
        "before_repo_status",
        "post_before_base_status",
        "model_patch_status",
        "test_patch_status",
    ):
        value = tests_status.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return False
    f2p_status = _direct_eval_plan_status(
        tests_status,
        "fail_to_pass",
        expected_f2p_plan,
        require_commands=True,
    )
    p2p_status = _direct_eval_plan_status(
        tests_status,
        "pass_to_pass",
        expected_p2p_plan,
        require_commands=False,
    )
    if f2p_status is None or p2p_status is None:
        return False
    if not _runtime_file_dependency_proof(
        payload,
        tests_status,
        expected_f2p_plan,
        expected_p2p_plan,
    ):
        return False
    expected_resolved = bool(f2p_status == 0 and p2p_status == 0)
    expected_outcome = "resolved" if expected_resolved else "unresolved"
    return bool(
        payload["resolved"] is expected_resolved
        and payload.get("outcome", expected_outcome) == expected_outcome
    )


def workflow_result(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    value = row.get("workflow_result")
    return value if isinstance(value, dict) else {}


def metric_status(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    result = workflow_result(row)
    return str(row.get("workflow_status") or result.get("status") or "")


def metric_done_with_advisory_gap(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    result = workflow_result(row)
    return bool(row.get("done_with_advisory_gap") or result.get("done_with_advisory_gap"))


def task_ids(predictions: list[dict[str, Any]], metrics: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in [*predictions, *metrics]:
        task_id = row_task_id(row)
        if task_id and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered


def latest_paired_rows(
    predictions: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    task_id: str,
) -> PairedRows:
    matched_predictions = [row for row in predictions if row_task_id(row) == task_id]
    matched_metrics = [row for row in metrics if row_task_id(row) == task_id]
    if not matched_predictions:
        return PairedRows(None, matched_metrics[-1] if matched_metrics else None, "missing_prediction")

    prediction = matched_predictions[-1]
    record_id = row_record_id(prediction)
    current_sha = row_patch_sha(prediction)

    if record_id:
        record_metrics = [row for row in matched_metrics if row_record_id(row) == record_id]
        if record_metrics:
            metric = record_metrics[-1]
            metric_sha = row_patch_sha(metric)
            if current_sha and metric_sha and not patch_sha_matches(metric_sha, current_sha):
                return PairedRows(prediction, None, "record_id_patch_sha_mismatch")
            if current_sha and not metric_sha:
                return PairedRows(prediction, None, "record_id_patch_sha_missing")
            return PairedRows(prediction, metric, "record_id")
        embedded_metric = embedded_workflow_metric(prediction)
        if embedded_metric is not None:
            return PairedRows(prediction, embedded_metric, "embedded_metric")
        return PairedRows(prediction, None, "missing_metric_for_record_id")

    if current_sha:
        for metric in reversed(matched_metrics):
            metric_sha = row_patch_sha(metric)
            if metric_sha and patch_sha_matches(metric_sha, current_sha):
                return PairedRows(prediction, metric, "patch_sha")
        embedded_metric = embedded_workflow_metric(prediction)
        if embedded_metric is not None:
            return PairedRows(prediction, embedded_metric, "embedded_metric")
        # A patch body is itself an identity even when legacy producers did
        # not persist an explicit ``patch_sha256`` field.  Once that identity
        # is available, an unbound legacy metric is not safe to reuse: it may
        # belong to an older candidate for the same task.
        return PairedRows(prediction, None, "missing_metric_for_patch_sha")

    legacy_metrics = [row for row in matched_metrics if not row_record_id(row) and not row_explicit_patch_sha(row)]
    if legacy_metrics:
        return PairedRows(prediction, legacy_metrics[-1], "legacy_latest")
    return PairedRows(prediction, None, "missing_metric")
