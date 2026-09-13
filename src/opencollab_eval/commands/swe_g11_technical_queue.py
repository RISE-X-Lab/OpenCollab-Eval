"""Task-level technical recovery planning for the G1.1 parallel runner."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opencollab_eval.commands import _swe_report_io
from opencollab_eval.engine.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
)
from opencollab_eval.engine.swe_generation_proof import (
    current_generation_summary_proof_valid,
)
from opencollab_eval.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
)

RecoveryMode = Literal["generation", "eval_only"]
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_MAX_DECISION_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    """Immutable identity needed to evaluate one existing candidate safely."""

    task: str
    record_id: str
    source_patch_sha256: str
    eval_patch_sha256: str
    base_run_dir: str


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    """One unique task-level recovery identity."""

    index: int
    ordinal: int
    mode: RecoveryMode
    run_id: str
    base_run_dir: str
    output_dir: Path
    parent_report: Path
    parent_report_sha256: str
    max_eval_attempts: int
    candidate: CandidateBinding | None = None

    @property
    def attempt_id(self) -> str:
        return f"task-{self.index}-technical-{self.ordinal}-{self.mode}"

    @property
    def json_report(self) -> Path:
        return self.output_dir / "report.json"

    @property
    def markdown_report(self) -> Path:
        return self.output_dir / "report.md"

    @property
    def stdout_log(self) -> Path:
        return self.output_dir / "stdout.log"

    @property
    def stderr_log(self) -> Path:
        return self.output_dir / "stderr.log"

    @property
    def decision_path(self) -> Path:
        return self.output_dir / "decision.json"

    @property
    def eval_dir_name(self) -> str:
        return f"technical_recovery_{self.ordinal}"

    def session_prefix(self, configured: str) -> str:
        return f"{configured}_tr{self.index}_{self.ordinal}_{self.mode}"


def _regular_sha256(path: Path) -> str:
    try:
        payload = _swe_report_io.read_bytes(path, max_bytes=_swe_report_io.MAX_REPORT_BYTES)
    except (FileNotFoundError, OSError, ValueError):
        return ""
    return hashlib.sha256(payload).hexdigest()


def _one_row(result: dict[str, Any]) -> dict[str, Any] | None:
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _candidate_fields_present(generation: dict[str, Any]) -> bool:
    try:
        patch_len = int(generation.get("patch_len") or 0)
    except (TypeError, ValueError):
        return False
    source_sha = str(generation.get("source_patch_sha256") or generation.get("patch_sha256") or "")
    eval_sha = str(generation.get("eval_patch_sha256") or generation.get("patch_sha256") or "")
    return bool(
        patch_len > 0
        and source_sha != _EMPTY_SHA256
        and eval_sha != _EMPTY_SHA256
        and _SHA256_RE.fullmatch(source_sha)
        and _SHA256_RE.fullmatch(eval_sha)
    )


def candidate_binding_from_result(
    result: dict[str, Any],
) -> CandidateBinding | None:
    """Return a proven non-empty candidate binding from one technical result."""
    row = _one_row(result)
    if row is None:
        return None
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    task = str(row.get("task") or generation.get("task") or "").strip()
    record_id = str(generation.get("record_id") or "").strip()
    source_sha = str(generation.get("source_patch_sha256") or generation.get("patch_sha256") or "")
    eval_sha = str(generation.get("eval_patch_sha256") or generation.get("patch_sha256") or "")
    base_run_dir = str(result.get("base_run_dir") or result.get("summary_base_run_dir") or "").strip()
    if not base_run_dir:
        report = result.get("summary")
        if isinstance(report, dict):
            base_run_dir = str(report.get("base_run_dir") or "").strip()
    if not (
        generation.get("status") == "generation_done"
        and generation.get("submission_integrity") == SUBMISSION_INTEGRITY_PROVEN
        and generation.get("execution_quiesced") is True
        and current_generation_summary_proof_valid(generation)
        and task
        and record_id
        and base_run_dir
        and _candidate_fields_present(generation)
    ):
        return None
    return CandidateBinding(
        task=task,
        record_id=record_id,
        source_patch_sha256=source_sha,
        eval_patch_sha256=eval_sha,
        base_run_dir=base_run_dir,
    )


def result_has_nonempty_candidate_evidence(result: dict[str, Any]) -> bool:
    """Detect candidate traces that are unsafe to regenerate or evaluate."""
    row = _one_row(result)
    if row is None:
        return False
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    return _candidate_fields_present(generation)


def recovery_block_reason(result: dict[str, Any]) -> str:
    """Return why a technical result must not be automatically recovered."""
    if not int(result.get("technical_failed") or 0):
        return "not_technical"
    scope = str(result.get("failure_scope") or "")
    probe = result.get("failure_probe") if isinstance(result.get("failure_probe"), dict) else {}
    if scope == "shared_infrastructure" and probe.get("direct") is True and probe.get("status") == "failed":
        return "shared_infrastructure"
    if scope == "image":
        return "image_scope"
    if result_has_nonempty_candidate_evidence(result) and not candidate_binding_from_result(result):
        return "candidate_identity_unproven"
    return ""


def confirm_eval_only_runtime_after_failure(
    config: Any,
    result: dict[str, Any],
    *,
    run_runtime_health: Callable[[Any], dict[str, Any]],
    shared_probe_failure: type[Exception],
) -> dict[str, Any]:
    """Classify eval-only health without exposing a model-probe callback."""
    if config.skip_health_checks or config.dry_run:
        return result
    if result.get("completed") and not int(result.get("technical_failed") or 0):
        return result
    try:
        runtime_probe = run_runtime_health(config)
    except shared_probe_failure as exc:
        result["failure_scope"] = "shared_infrastructure"
        result["failure_probe"] = {
            "direct": True,
            "status": "failed",
            "evidence": getattr(exc, "result", {}),
            "error_type": type(exc).__name__,
            "model_probe": "forbidden_eval_only",
        }
        return result
    except Exception as exc:  # noqa: BLE001 - probe setup failure stays task-scoped
        result.setdefault("failure_scope", "task")
        result["failure_probe"] = {
            "direct": False,
            "status": "setup_error",
            "error_type": type(exc).__name__,
            "model_probe": "forbidden_eval_only",
        }
        return result
    result.setdefault("failure_scope", "task")
    result["failure_probe"] = {
        "direct": True,
        "status": "passed",
        "runtime": runtime_probe,
        "model_probe": "forbidden_eval_only",
    }
    return result


def plan_recovery_attempt(
    *,
    config: Any,
    result: dict[str, Any],
    ordinal: int,
) -> RecoveryAttempt | None:
    """Plan one unique recovery while keeping the original report immutable."""
    if recovery_block_reason(result):
        return None
    index = int(result["index"])
    candidate = candidate_binding_from_result(result)
    mode: RecoveryMode = "eval_only" if candidate is not None else "generation"
    run_id = f"{config.run_id}_task{index}_technical_{mode}_{ordinal}"
    output_dir = config.output_dir / "technical_recovery" / f"task_{index}" / f"attempt_{ordinal}_{mode}"
    base_run_dir = (
        (f"{config.remote_base}/_technical_recovery/task_{index}/eval_only_{ordinal}")
        if candidate is not None
        else (f"{config.remote_base}/_technical_recovery/task_{index}/generation_{ordinal}")
    )
    parent_report = Path(str(result.get("json_report") or ""))
    return RecoveryAttempt(
        index=index,
        ordinal=ordinal,
        mode=mode,
        run_id=run_id,
        base_run_dir=base_run_dir,
        output_dir=output_dir,
        parent_report=parent_report,
        parent_report_sha256=_regular_sha256(parent_report),
        max_eval_attempts=(min(2, max(0, int(result.get("eval_attempts") or 0)) + 1) if candidate is not None else 1),
        candidate=candidate,
    )


def decision_payload(
    attempt: RecoveryAttempt,
    *,
    result: dict[str, Any],
    runtime_tree_sha256: str,
) -> dict[str, Any]:
    candidate = attempt.candidate
    return {
        "schema": "opencollab.swe_g11_technical_recovery_decision.v1",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "attempt_id": attempt.attempt_id,
        "index": attempt.index,
        "ordinal": attempt.ordinal,
        "mode": attempt.mode,
        "run_id": attempt.run_id,
        "base_run_dir": attempt.base_run_dir,
        "parent_report": str(attempt.parent_report),
        "parent_report_sha256": attempt.parent_report_sha256,
        "parent_runner_status": result.get("runner_status"),
        "failure_scope": result.get("failure_scope"),
        "failure_probe": result.get("failure_probe") or {},
        "runtime_tree_sha256": runtime_tree_sha256,
        "max_eval_attempts": attempt.max_eval_attempts,
        "candidate": (
            {
                "task": candidate.task,
                "record_id": candidate.record_id,
                "source_patch_sha256": candidate.source_patch_sha256,
                "eval_patch_sha256": candidate.eval_patch_sha256,
                "source_base_run_dir": candidate.base_run_dir,
            }
            if candidate is not None
            else None
        ),
    }


def write_decision_once(path: Path, payload: dict[str, Any]) -> None:
    """Create an immutable decision or verify an identical existing decision."""
    ensure_directory_no_symlinks(path.parent)
    existing, error = _swe_report_io.load_json_with_error(path)
    if error is None:
        stable_existing = {key: value for key, value in existing.items() if key != "created_at"}
        stable_payload = {key: value for key, value in payload.items() if key != "created_at"}
        if stable_existing != stable_payload:
            raise RuntimeError(f"technical recovery decision identity mismatch: {path}")
        return
    if error != "missing_report_file":
        raise RuntimeError(f"technical recovery decision is unsafe: {path}")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    create_regular_bytes_atomic(
        path,
        encoded,
        max_bytes=_MAX_DECISION_BYTES,
    )


def write_manifest(output_dir: Path, events: list[dict[str, Any]]) -> None:
    """Publish the cumulative recovery ledger atomically."""
    _swe_report_io.write_json(
        output_dir / "technical_recovery_manifest.json",
        {
            "schema": "opencollab.swe_g11_technical_recovery_manifest.v1",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "events": events,
        },
    )


def load_manifest_events(output_dir: Path) -> list[dict[str, Any]]:
    """Load a compatible cumulative ledger for controller resumption."""
    path = output_dir / "technical_recovery_manifest.json"
    payload, error = _swe_report_io.load_json_with_error(path)
    if error == "missing_report_file":
        return []
    if error is not None:
        raise RuntimeError("technical recovery manifest is unsafe")
    if payload.get("schema") != "opencollab.swe_g11_technical_recovery_manifest.v1":
        raise RuntimeError("technical recovery manifest schema mismatch")
    events = payload.get("events")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise RuntimeError("technical recovery manifest events are invalid")
    return list(events)


def artifact_sha256(path: Path) -> str:
    """Return the digest of one regular recovery artifact when present."""
    return _regular_sha256(path)


def record_event(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    """Append one state transition exactly once."""
    identity = (event.get("attempt_id"), event.get("state"), event.get("index"))
    if not any((item.get("attempt_id"), item.get("state"), item.get("index")) == identity for item in events):
        events.append(event)


def select_recovery_result(
    result: dict[str, Any],
    *,
    previous: dict[str, Any],
    attempt: RecoveryAttempt,
) -> dict[str, Any]:
    """Select a recovery result while retaining all prior attempt identities."""
    old = previous.get("technical_recovery") if isinstance(previous.get("technical_recovery"), dict) else {}
    attempts = list(old.get("attempts") or [])
    attempts.append(
        {
            "attempt_id": attempt.attempt_id,
            "ordinal": attempt.ordinal,
            "mode": attempt.mode,
            "run_id": attempt.run_id,
            "base_run_dir": attempt.base_run_dir,
            "decision": str(attempt.decision_path),
            "report": str(attempt.json_report),
            "report_sha256": artifact_sha256(attempt.json_report),
            "runner_status": result.get("runner_status"),
            "technical_failed": int(result.get("technical_failed") or 0),
        }
    )
    return {
        **result,
        "technical_recovery": {
            "enabled": True,
            "selected_attempt_id": attempt.attempt_id,
            "attempts": attempts,
        },
    }


def record_exhausted(
    events: list[dict[str, Any]],
    selected: dict[int, dict[str, Any]],
    indices: tuple[int, ...],
    max_recoveries: int,
) -> None:
    """Record task-level technical results that consumed their full budget."""
    for index in indices:
        result = selected[index]
        if int(result.get("technical_failed") or 0) and not recovery_block_reason(result):
            record_event(
                events,
                {
                    "state": "exhausted",
                    "index": index,
                    "ordinal": max_recoveries,
                    "reason": "recovery_budget_exhausted",
                },
            )


__all__ = [
    "CandidateBinding",
    "RecoveryAttempt",
    "artifact_sha256",
    "candidate_binding_from_result",
    "confirm_eval_only_runtime_after_failure",
    "decision_payload",
    "load_manifest_events",
    "plan_recovery_attempt",
    "record_event",
    "record_exhausted",
    "recovery_block_reason",
    "result_has_nonempty_candidate_evidence",
    "select_recovery_result",
    "write_decision_once",
    "write_manifest",
]
