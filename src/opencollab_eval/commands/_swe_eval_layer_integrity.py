"""Identity and execution-proof checks for SWE evaluation fact reports."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from opencollab_eval.engine.swe_eval_discovery import (
    _direct_eval_done_has_execution_proof,
)
from opencollab_eval.engine.swe_generation_proof import (
    current_generation_summary_proof_valid,
)

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
EMPTY_PATCH_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class AttemptIntegrity:
    """Normalized identity and direct-execution evidence for one task row."""

    record_id: str
    patch_sha256: str
    eval_patch_sha256: str
    filtered_patch_paths: tuple[str, ...]
    direct_execution_proven: bool
    reasons: tuple[str, ...]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _full_sha(value: Any) -> str:
    text = str(value or "")
    return text.lower() if _SHA256_RE.fullmatch(text) else ""


def _append_once(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _filtered_paths(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or any(not isinstance(path, str) or not path for path in value):
        return None
    paths = tuple(value)
    return paths if len(paths) == len(set(paths)) else None


def _empty_patch_integrity(row: dict[str, Any], task: str) -> AttemptIntegrity:
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    reasons: list[str] = []
    if generation.get("status") != "empty_patch":
        reasons.append("empty_patch_generation_status_invalid")
    patch_len = generation.get("patch_len")
    if isinstance(patch_len, bool) or patch_len != 0:
        reasons.append("empty_patch_length_invalid")
    if str(generation.get("task") or "") != task:
        reasons.append("generation_task_mismatch")
    record_id = str(generation.get("record_id") or "")
    if not record_id:
        reasons.append("missing_generation_record_id")
    patch_sha256 = _full_sha(generation.get("patch_sha256"))
    if patch_sha256 != EMPTY_PATCH_SHA256:
        reasons.append("empty_patch_sha256_invalid")
    if generation.get("workflow_status") != "empty_patch_after_done":
        reasons.append("empty_patch_workflow_status_invalid")
    if generation.get("submission_integrity") != "empty_patch_proven":
        reasons.append("empty_patch_integrity_unproven")
    expected_integrity_fields = {
        "submission_eligible": False,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": False,
    }
    for field, expected in expected_integrity_fields.items():
        if generation.get(field) is not expected:
            reasons.append(f"empty_patch_integrity_field_invalid:{field}")
    extraction = _mapping(generation.get("trusted_patch_extraction"))
    if (
        not current_generation_summary_proof_valid(generation)
        or extraction.get("patch_bytes") != 0
    ):
        reasons.append("missing_trusted_empty_patch_proof")
    if str(evaluation.get("task") or "") != task:
        reasons.append("evaluation_task_mismatch")
    if evaluation.get("status") != "skipped_empty_patch":
        reasons.append("empty_patch_eval_status_invalid")
    if evaluation.get("summary") not in (None, {}):
        reasons.append("empty_patch_eval_summary_not_empty")
    if evaluation.get("executed") is True:
        reasons.append("empty_patch_eval_execution_conflict")
    if "attempt_count" in evaluation:
        attempt_count = evaluation.get("attempt_count")
        if isinstance(attempt_count, bool) or attempt_count != 0:
            reasons.append("empty_patch_eval_attempt_count_invalid")
    return AttemptIntegrity(
        record_id=record_id,
        patch_sha256=patch_sha256,
        eval_patch_sha256="",
        filtered_patch_paths=(),
        direct_execution_proven=False,
        reasons=tuple(reasons),
    )


def declared_empty_patch(row: dict[str, Any]) -> bool:
    task = str(row.get("task") or row.get("instance_id") or "")
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    claims_empty = bool(
        generation.get("status") == "empty_patch"
        or evaluation.get("status") == "skipped_empty_patch"
    )
    return bool(claims_empty and not _empty_patch_integrity(row, task).reasons)


def attempt_integrity(row: dict[str, Any], task: str) -> AttemptIntegrity:
    """Require a full generation/evaluation identity and executable terminal proof."""
    generation = _mapping(row.get("generation"))
    evaluation = _mapping(row.get("eval"))
    generation_status = str(generation.get("status") or "")
    eval_status = str(evaluation.get("status") or "")
    if generation_status == "would_generate" and eval_status == "would_eval":
        return AttemptIntegrity("", "", "", (), False, ())
    if (
        generation_status == "empty_patch"
        or eval_status == "skipped_empty_patch"
    ):
        return _empty_patch_integrity(row, task)
    summary = _mapping(evaluation.get("summary"))
    reasons: list[str] = []

    if generation_status != "generation_done":
        _append_once(
            reasons,
            "missing_generation_status"
            if not generation_status
            else f"unexpected_generation_status:{generation_status}",
        )
    if str(generation.get("task") or "") != task:
        _append_once(reasons, "generation_task_mismatch")
    record_id = str(generation.get("record_id") or "")
    if not record_id:
        _append_once(reasons, "missing_generation_record_id")
    generation_sha = _full_sha(generation.get("patch_sha256"))
    if not generation_sha:
        _append_once(reasons, "invalid_generation_patch_sha256")
    extraction = _mapping(generation.get("trusted_patch_extraction"))
    if generation_sha == EMPTY_PATCH_SHA256 or extraction.get("patch_bytes") == 0:
        _append_once(reasons, "undeclared_empty_patch")
    if not current_generation_summary_proof_valid(generation):
        _append_once(reasons, "missing_trusted_generation_proof")
    generation_eval_sha = _full_sha(generation.get("eval_patch_sha256"))
    generation_filtered_paths = _filtered_paths(generation.get("filtered_patch_paths"))

    if evaluation and str(evaluation.get("task") or "") != task:
        _append_once(reasons, "evaluation_task_mismatch")

    terminal_or_recorded = bool(summary) or eval_status in {
        "eval_done",
        "technical_eval_failed",
        "empty_eval_patch_invalid",
        "blocked_missing_eval_spec",
    }
    summary_record_id = str(summary.get("record_id") or "")
    summary_sha = _full_sha(summary.get("patch_sha256"))
    summary_eval_sha = _full_sha(summary.get("eval_patch_sha256"))
    summary_filtered_paths = _filtered_paths(summary.get("filtered_patch_paths"))
    if terminal_or_recorded:
        if str(summary.get("task") or "") != task:
            _append_once(reasons, "eval_summary_task_mismatch")
        if not summary_record_id:
            _append_once(reasons, "missing_eval_record_id")
        elif record_id and summary_record_id != record_id:
            _append_once(reasons, "eval_record_id_mismatch")
        if not summary_sha:
            _append_once(reasons, "invalid_eval_source_patch_sha256")
        elif generation_sha and summary_sha != generation_sha:
            _append_once(reasons, "eval_source_patch_sha256_mismatch")
        if not generation_eval_sha:
            _append_once(reasons, "invalid_generation_eval_patch_sha256")
        if not summary_eval_sha:
            _append_once(reasons, "invalid_eval_patch_sha256")
        elif generation_eval_sha and summary_eval_sha != generation_eval_sha:
            _append_once(reasons, "eval_patch_sha256_mismatch")
        if generation_filtered_paths is None:
            _append_once(reasons, "invalid_generation_filtered_patch_paths")
        if summary_filtered_paths is None:
            _append_once(reasons, "invalid_eval_filtered_patch_paths")
        elif (
            generation_filtered_paths is not None
            and summary_filtered_paths != generation_filtered_paths
        ):
            _append_once(reasons, "filtered_patch_paths_mismatch")

    direct_execution_proven = bool(
        eval_status == "eval_done"
        and _direct_eval_done_has_execution_proof(summary)
    )
    if eval_status == "eval_done" and not direct_execution_proven:
        _append_once(reasons, "missing_direct_execution_proof")
    if eval_status == "technical_eval_failed" and summary.get("resolved") is True:
        _append_once(reasons, "technical_resolved_conflict")

    return AttemptIntegrity(
        record_id=record_id,
        patch_sha256=generation_sha or summary_sha,
        eval_patch_sha256=generation_eval_sha or summary_eval_sha,
        filtered_patch_paths=generation_filtered_paths or summary_filtered_paths or (),
        direct_execution_proven=direct_execution_proven,
        reasons=tuple(reasons),
    )


def strict_index(value: Any) -> int | None:
    """Return an integer task index while rejecting booleans and lossy strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value)
    return None


def eval_attempt_count(row: dict[str, Any]) -> int:
    """Return the number of genuine official-eval starts represented by one row."""
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    if evaluation.get("executed") is False:
        return 0
    value = evaluation.get("attempt_count")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def claims_evidence_only_rejudgement(
    report: dict[str, Any],
    row: dict[str, Any],
) -> bool:
    reconciliation = report.get("rejudgement")
    evaluation = row.get("eval")
    summary = evaluation.get("summary") if isinstance(evaluation, dict) else None
    rejudgement = summary.get("rejudgement") if isinstance(summary, dict) else None
    return bool(
        isinstance(reconciliation, dict)
        and reconciliation.get("schema") == "opencollab.eval_only_reconciliation.v1"
        and isinstance(evaluation, dict)
        and evaluation.get("status") == "eval_done"
        and evaluation.get("executed") is False
        and isinstance(rejudgement, dict)
        and rejudgement.get("schema") == "opencollab.prolite_direct_eval_rejudgement.v1"
    )


def report_is_evidence_only_rejudgement(report: dict[str, Any]) -> bool:
    rows = report.get("rows")
    return bool(
        isinstance(rows, list)
        and rows
        and all(
            isinstance(row, dict) and claims_evidence_only_rejudgement(report, row)
            for row in rows
        )
    )


def report_evidence_order(item: tuple[Any, dict[str, Any], Any]) -> tuple[bool, str]:
    return report_is_evidence_only_rejudgement(item[1]), str(item[0])


def row_attempt_identity(row: dict[str, Any]) -> dict[str, str] | None:
    generation = row.get("generation")
    evaluation = row.get("eval")
    summary = evaluation.get("summary") if isinstance(evaluation, dict) else None
    if not isinstance(generation, dict) or not isinstance(summary, dict):
        return None
    identity = {
        "task": str(row.get("task") or ""),
        "record_id": str(generation.get("record_id") or ""),
        "patch_sha256": str(
            generation.get("source_patch_sha256")
            or generation.get("patch_sha256")
            or ""
        ),
        "eval_patch_sha256": str(
            generation.get("eval_patch_sha256")
            or summary.get("eval_patch_sha256")
            or ""
        ),
        "eval_spec_sha256": str(summary.get("eval_spec_sha256") or ""),
        "eval_image_id": str(summary.get("eval_image_id") or ""),
    }
    return identity if all(identity.values()) else None


def evidence_only_rejudgement_binding(
    report: dict[str, Any],
    row: dict[str, Any],
) -> tuple[dict[str, str], int] | None:
    """Bind a derived verdict to the exact preceding official-eval ledger."""
    reconciliation = report.get("rejudgement")
    evaluation = row.get("eval")
    summary = evaluation.get("summary") if isinstance(evaluation, dict) else None
    rejudgement = summary.get("rejudgement") if isinstance(summary, dict) else None
    matching = (
        rejudgement.get("matching_eval_attempts")
        if isinstance(rejudgement, dict)
        else None
    )
    represented = evaluation.get("attempt_count") if isinstance(evaluation, dict) else None
    identity = row_attempt_identity(row)
    if not (
        isinstance(reconciliation, dict)
        and reconciliation.get("schema") == "opencollab.eval_only_reconciliation.v1"
        and claims_evidence_only_rejudgement(report, row)
        and isinstance(rejudgement, dict)
        and rejudgement.get("schema")
        == "opencollab.prolite_direct_eval_rejudgement.v1"
        and rejudgement.get("added_eval_attempts") == 0
        and not isinstance(matching, bool)
        and isinstance(matching, int)
        and matching > 0
        and not isinstance(represented, bool)
        and isinstance(represented, int)
        and represented == matching
        and identity is not None
        and rejudgement.get("attempt_identity") == identity
    ):
        return None
    return identity, matching


def evidence_only_source_round(
    report: dict[str, Any],
    row: dict[str, Any],
    task: str,
    observed_attempts: list[dict[str, Any]],
) -> int | None:
    """Find the real official-eval round represented by a derived verdict."""
    binding = evidence_only_rejudgement_binding(report, row)
    if binding is None:
        return None
    identity, matching_count = binding
    rounds = [
        attempt["round"]
        for attempt in observed_attempts
        if attempt["task"] == task
        and eval_attempt_count(attempt["row"]) == matching_count
        and row_attempt_identity(attempt["row"]) == identity
    ]
    return max(rounds) if rounds else None


def _eval_ledger_key(attempt: dict[str, Any]) -> tuple[str, str, str]:
    task = str(attempt.get("task") or "")
    generation_log = str(attempt.get("generation_log") or "")
    if generation_log:
        path = PurePosixPath(generation_log)
        if path.parent.name == "generation_logs":
            return (task, "candidate_root", str(path.parent.parent))
    report_path = str(attempt.get("report_path") or "")
    if report_path:
        path = PurePosixPath(report_path)
        if len(path.parents) >= 4 and path.parents[1].name == "reports":
            return (task, "candidate_root", str(path.parents[3]))
    if generation_log:
        return (task, "generation_log", generation_log)
    return (task, "source_report", str(attempt.get("source_report") or ""))


def eval_attempt_increments(
    attempts: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], int]]:
    """Return per-record increments across cumulative candidate ledgers."""
    observed_by_ledger: dict[tuple[str, str, str], int] = {}
    increments = []
    for attempt in attempts:
        ledger = _eval_ledger_key(attempt)
        count = eval_attempt_count(attempt.get("row") or {})
        previous = observed_by_ledger.get(ledger, 0)
        increments.append((attempt, max(0, count - previous)))
        observed_by_ledger[ledger] = max(previous, count)
    return increments


def combined_eval_attempt_count(attempts: list[dict[str, Any]]) -> int:
    """Count each persisted candidate-ledger attempt exactly once."""
    return sum(increment for _attempt, increment in eval_attempt_increments(attempts))


def successful_pre_eval_recovery(
    attempt: dict[str, Any],
    observed_attempts: list[dict[str, Any]],
) -> bool:
    """Return whether a later verified eval supersedes a pre-eval classification failure."""
    if eval_attempt_count(attempt["row"]) or not attempt.get("record_id") or not attempt.get("patch_sha256"):
        return False
    raw_eval = attempt["row"].get("eval")
    if not isinstance(raw_eval, dict) or raw_eval.get("status") != "skipped_generation_not_ready":
        return False
    recoverable = {
        "unexpected_generation_status:generation_failed",
        "missing_trusted_generation_proof",
    }
    identity_reasons = set(attempt.get("identity_reasons") or ())
    if not identity_reasons or not identity_reasons.issubset(recoverable):
        return False
    return any(
        candidate.get("round", 0) > attempt.get("round", 0)
        and candidate.get("task") == attempt["task"]
        and candidate.get("record_id") == attempt["record_id"]
        and candidate.get("patch_sha256") == attempt["patch_sha256"]
        and candidate.get("generation_status") == "generation_done"
        and candidate.get("eval_success") is True
        and candidate.get("direct_execution_proven") is True
        and not candidate.get("identity_reasons")
        for candidate in observed_attempts
    )


def append_issue(container: dict[Any, list[str]], key: Any, reason: str) -> None:
    """Add one deterministic technical reason without duplicates."""
    reasons = container.setdefault(key, [])
    if reason not in reasons:
        reasons.append(reason)


def report_census(
    report: dict[str, Any],
) -> tuple[list[int], dict[int, list[str]], list[str]]:
    """Extract the declared task census and orchestrator-level failures."""
    expected: list[int] = []
    issues: dict[int, list[str]] = {}
    global_issues: list[str] = []
    raw_indices = report.get("indices")
    if raw_indices is not None:
        if not isinstance(raw_indices, list):
            global_issues.append("invalid_expected_index_census")
        else:
            for value in raw_indices:
                index = strict_index(value)
                if index is None:
                    if "invalid_expected_index_census" not in global_issues:
                        global_issues.append("invalid_expected_index_census")
                    continue
                if index in expected:
                    append_issue(issues, index, "duplicate_expected_index")
                else:
                    expected.append(index)

    results = report.get("results")
    if not isinstance(results, list):
        return expected, issues, global_issues
    seen_results: set[int] = set()
    for result in results:
        if not isinstance(result, dict):
            if "invalid_orchestrator_result" not in global_issues:
                global_issues.append("invalid_orchestrator_result")
            continue
        index = strict_index(result.get("index"))
        if index is None:
            if "invalid_orchestrator_result_index" not in global_issues:
                global_issues.append("invalid_orchestrator_result_index")
            continue
        if index in seen_results:
            append_issue(issues, index, "duplicate_orchestrator_result")
        seen_results.add(index)
        if str(result.get("runner_status") or "") == "orchestrator_exception":
            append_issue(issues, index, "orchestrator_exception")
        if result.get("completed") is False:
            append_issue(issues, index, "incomplete_orchestrator_result")
        rows = result.get("rows")
        if not isinstance(rows, list) or not rows:
            append_issue(issues, index, "missing_task_row")
            continue
        for row in rows:
            if not isinstance(row, dict):
                append_issue(issues, index, "invalid_task_row")
                continue
            row_index = strict_index(row.get("index"))
            if row_index != index:
                append_issue(issues, index, "orchestrator_row_index_mismatch")
                if row_index is not None:
                    append_issue(issues, row_index, "orchestrator_row_index_mismatch")
    return expected, issues, global_issues


def mark_technical(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    """Convert one selected fact row into a fail-closed technical result."""
    value = dict(record)
    previous_reasons = [str(reason) for reason in value.get("technical_reasons") or []]
    for reason in reasons:
        if reason not in previous_reasons:
            previous_reasons.append(reason)
    if value.get("eval_status") != "technical_report_inconsistency":
        value["accepted_eval_status"] = value.get("eval_status")
        value["accepted_resolved"] = value.get("resolved")
    value.update(
        {
            "eval_status": "technical_report_inconsistency",
            "eval_success": False,
            "eval_pending": False,
            "resolved": None,
            "technical_failed": True,
            "technical_reasons": previous_reasons,
        }
    )
    return value


def _missing_task_record(index: int, reasons: list[str]) -> dict[str, Any]:
    return mark_technical(
        {
            "round": 0,
            "source_order": 0,
            "source_report": None,
            "index": index,
            "task": f"expected-index-{index}",
            "generation_status": "missing_generation",
            "patch_len": None,
            "patch_sha256": "",
            "record_id": "",
            "direct_execution_proven": False,
            "report_path": None,
            "attempt_count": 0,
            "eval_attempt_count": 0,
            "attempts": [],
            "observed_record_count": 0,
            "observed_eval_attempt_count": 0,
            "token_cost": {},
            "technical_reasons": [],
        },
        ["missing_expected_task", *reasons],
    )


def apply_expected_census(
    tasks: list[dict[str, Any]],
    expected_indices: tuple[int, ...],
    issues_by_index: dict[int, list[str]],
    global_issues: list[str],
) -> list[dict[str, Any]]:
    """Return exactly one fail-closed task row for every expected index."""
    if not expected_indices:
        return tasks
    by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        index = strict_index(task.get("index"))
        if index is not None:
            by_index[index].append(task)

    expected_set = set(expected_indices)
    if any(index not in expected_set for index in by_index):
        if "unexpected_task_index" not in global_issues:
            global_issues.append("unexpected_task_index")

    result: list[dict[str, Any]] = []
    for index in expected_indices:
        candidates = sorted(by_index.get(index, []), key=lambda item: str(item.get("task") or ""))
        reasons = [*global_issues, *issues_by_index.get(index, [])]
        if not candidates:
            result.append(_missing_task_record(index, reasons))
            continue
        selected = candidates[0]
        if len(candidates) > 1:
            reasons.append("index_task_mapping_conflict")
            selected = dict(selected)
            selected["mapping_conflict_tasks"] = [item.get("task") for item in candidates]
        result.append(mark_technical(selected, reasons) if reasons else selected)
    return result


__all__ = [
    "AttemptIntegrity",
    "EMPTY_PATCH_SHA256",
    "append_issue",
    "apply_expected_census",
    "attempt_integrity",
    "declared_empty_patch",
    "combined_eval_attempt_count",
    "eval_attempt_increments",
    "mark_technical",
    "report_census",
    "strict_index",
]
