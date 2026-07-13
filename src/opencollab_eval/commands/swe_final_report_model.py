"""Validated comparison model for terminal SWE evaluation reports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencollab_eval import __version__
from opencollab_eval.commands import _swe_report_io as report_io
from opencollab_eval.commands.swe_final_report_dataset import DatasetTask, LoadedDataset
from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof

FACT_REPORT_SCHEMA = "opencollab.swe_eval_layer_final_report.v1"
AUDIT_MANIFEST_SCHEMA = "opencollab.swe_clean_run_manifest.v1"
AUDIT_EVIDENCE_SCHEMA = "opencollab.swe_clean_run_evidence.v1"
NARRATIVE_SCHEMA = "opencollab.swe_final_report_narrative.v1"
LABELS_SCHEMA = "opencollab.swe_final_report_labels.v1"
COMPARISON_SCHEMA = "opencollab.swe_final_comparison.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REQUIRED_TASK_ARTIFACTS = frozenset(
    {"official_report", "trajectory", "candidate_identity", "network_isolation"}
)


class FinalReportInputError(ValueError):
    """Raised when a claimed final report lacks terminal evidence."""


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    path: Path
    value: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class MethodFacts:
    name: str
    source: LoadedDocument
    resolved: tuple[int, ...]
    unresolved: tuple[int, ...]
    tasks: tuple[dict[str, Any], ...]


def _load_document(path: Path, *, label: str) -> LoadedDocument:
    try:
        text = report_io.read_text(path, max_bytes=report_io.MAX_REPORT_BYTES)
        value = json.loads(text)
    except FileNotFoundError as exc:
        raise FinalReportInputError(f"{label} is missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise FinalReportInputError(f"{label} is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FinalReportInputError(f"{label} is not valid JSON: {path}") from exc
    except (OSError, ValueError) as exc:
        raise FinalReportInputError(f"{label} is unsafe or unstable: {path}") from exc
    if not isinstance(value, dict):
        raise FinalReportInputError(f"{label} root must be an object: {path}")
    return LoadedDocument(
        path=path,
        value=value,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _require_exact_indices(value: Any, expected: tuple[int, ...], *, label: str) -> None:
    if value != list(expected):
        raise FinalReportInputError(f"{label} must contain the exact ordered task census")


def _require_count(counts: dict[str, Any], key: str, expected: int, *, label: str) -> None:
    value = counts.get(key)
    if isinstance(value, bool) or value != expected:
        raise FinalReportInputError(f"{label} count {key!r} must equal {expected}")


def _read_bound_artifact(
    reference: Any,
    *,
    anchor: Path,
    label: str,
    verified: set[tuple[str, str]],
    payload_cache: dict[tuple[str, str], bytes],
    retain_payload: bool,
) -> tuple[str, str, bytes | None]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise FinalReportInputError(f"{label} must contain only path and sha256")
    raw_path = reference.get("path")
    expected_sha = str(reference.get("sha256") or "").lower()
    if not isinstance(raw_path, str) or not raw_path or _SHA256_RE.fullmatch(expected_sha) is None:
        raise FinalReportInputError(f"{label} is invalid")
    artifact_path = Path(raw_path)
    if not artifact_path.is_absolute():
        artifact_path = anchor / artifact_path
    cache_key = (str(artifact_path), expected_sha)
    payload = payload_cache.get(cache_key) if retain_payload else None
    if cache_key not in verified or retain_payload and payload is None:
        try:
            payload = report_io.read_bytes(artifact_path, max_bytes=report_io.MAX_REPORT_BYTES)
        except (OSError, ValueError) as exc:
            raise FinalReportInputError(f"{label} is missing, unsafe, or unstable: {raw_path}") from exc
        if not payload:
            raise FinalReportInputError(f"{label} is empty: {raw_path}")
        if hashlib.sha256(payload).hexdigest() != expected_sha:
            raise FinalReportInputError(f"{label} hash changed: {raw_path}")
        verified.add(cache_key)
        if retain_payload:
            payload_cache.clear()
            payload_cache[cache_key] = payload
    return raw_path, expected_sha, payload if retain_payload else None


def _official_task_payload(payload: bytes, *, task: str, label: str) -> dict[str, Any]:
    try:
        root = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalReportInputError(f"{label} is not valid JSON") from exc
    if not isinstance(root, dict):
        raise FinalReportInputError(f"{label} root must be an object")
    candidate = root.get(task)
    if not isinstance(candidate, dict):
        candidate = root
    reported_task = str(
        candidate.get("instance_id") or candidate.get("task_id") or candidate.get("task") or ""
    )
    if reported_task != task:
        raise FinalReportInputError(f"{label} is bound to a different task")
    return candidate


def _verify_official_report(
    payload: bytes,
    *,
    fact: dict[str, Any],
    dataset_task: DatasetTask,
    label: str,
) -> None:
    report = _official_task_payload(payload, task=fact["task"], label=label)
    if _IMAGE_ID_RE.fullmatch(str(report.get("eval_image_id") or "").lower()) is None:
        raise FinalReportInputError(f"{label} lacks an immutable evaluation image identity")
    tests_status = report.get("tests_status")
    if not isinstance(tests_status, dict):
        raise FinalReportInputError(f"{label} has no tests_status object")
    f2p_plan = tests_status.get("fail_to_pass_plan")
    p2p_plan = tests_status.get("pass_to_pass_plan")
    for plan, targets, trusted_plan, plan_label in (
        (
            f2p_plan,
            dataset_task.fail_to_pass,
            dataset_task.fail_to_pass_plan,
            "FAIL_TO_PASS",
        ),
        (
            p2p_plan,
            dataset_task.pass_to_pass,
            dataset_task.pass_to_pass_plan,
            "PASS_TO_PASS",
        ),
    ):
        if not isinstance(plan, dict) or plan.get("schema") != "opencollab.prolite_test_plan.v2":
            raise FinalReportInputError(f"{label} has an invalid {plan_label} plan")
        if plan.get("declared_targets") != list(targets):
            raise FinalReportInputError(
                f"{label} {plan_label} targets do not match the trusted dataset"
            )
        normalized_plan = json.loads(json.dumps(plan))
        for proof in normalized_plan.get("proofs", []):
            if isinstance(proof, dict):
                proof.pop("candidate_source_paths", None)
        if normalized_plan != trusted_plan:
            raise FinalReportInputError(
                f"{label} {plan_label} plan does not match the trusted dataset adapter"
            )
    if not direct_eval_done_has_execution_proof(
        report,
        expected_f2p_plan=f2p_plan,
        expected_p2p_plan=p2p_plan,
    ):
        raise FinalReportInputError(f"{label} lacks executable target-test proof")
    if str(report.get("record_id") or "") != fact["record_id"]:
        raise FinalReportInputError(f"{label} has a mismatched record_id")
    report_patch_sha = str(report.get("patch_sha256") or "").lower()
    if report_patch_sha != fact["patch_sha256"]:
        raise FinalReportInputError(f"{label} has a mismatched patch_sha256")
    expected_resolved = fact["status"] == "resolved"
    if report.get("resolved") is not expected_resolved:
        raise FinalReportInputError(f"{label} has a mismatched verdict")


def load_method_facts(
    path: Path,
    *,
    name: str,
    expected: tuple[int, ...],
    dataset_tasks: tuple[DatasetTask, ...],
) -> MethodFacts:
    """Load one fact report and require a complete terminal partition."""

    document = _load_document(path, label=f"{name} fact report")
    report = document.value
    if report.get("schema") != FACT_REPORT_SCHEMA:
        raise FinalReportInputError(f"{name} fact report has an unsupported schema")
    _require_exact_indices(report.get("expected_indices"), expected, label=f"{name} expected_indices")
    if report.get("census_errors") not in (None, []):
        raise FinalReportInputError(f"{name} fact report contains census errors")
    raw_tasks = report.get("tasks")
    if not isinstance(raw_tasks, list) or len(raw_tasks) != len(expected):
        raise FinalReportInputError(f"{name} fact report must contain {len(expected)} task rows")
    if len(dataset_tasks) != len(expected):
        raise FinalReportInputError(f"{name} trusted dataset census has the wrong size")

    tasks: list[dict[str, Any]] = []
    indices: list[int] = []
    task_ids: set[str] = set()
    record_ids: set[str] = set()
    resolved: list[int] = []
    unresolved: list[int] = []
    for position, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise FinalReportInputError(f"{name} task row {position + 1} is not an object")
        index = raw_task.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index not in expected:
            raise FinalReportInputError(f"{name} task row {position + 1} has an invalid index")
        task_id = raw_task.get("task")
        if not isinstance(task_id, str) or not task_id.strip():
            raise FinalReportInputError(f"{name} task {index} has no stable task identity")
        dataset_task = dataset_tasks[position]
        if dataset_task.index != index or dataset_task.task != task_id:
            raise FinalReportInputError(
                f"{name} task {index} does not match the trusted dataset census"
            )
        if task_id in task_ids:
            raise FinalReportInputError(f"{name} task identity is duplicated: {task_id}")
        task_ids.add(task_id)
        indices.append(index)
        if raw_task.get("generation_status") != "generation_done":
            raise FinalReportInputError(f"{name} task {index} has no completed generation")
        if raw_task.get("eval_status") != "eval_done" or raw_task.get("eval_success") is not True:
            raise FinalReportInputError(f"{name} task {index} has no successful official evaluation")
        if raw_task.get("eval_pending") is not False:
            raise FinalReportInputError(f"{name} task {index} is still pending")
        if raw_task.get("technical_failed") is not False:
            raise FinalReportInputError(f"{name} task {index} is technical")
        if raw_task.get("technical_reasons") not in (None, []):
            raise FinalReportInputError(f"{name} task {index} retains technical reasons")
        verdict = raw_task.get("resolved")
        if not isinstance(verdict, bool):
            raise FinalReportInputError(f"{name} task {index} has no Boolean verdict")
        if raw_task.get("direct_execution_proven") is not True:
            raise FinalReportInputError(f"{name} task {index} lacks direct execution proof")
        record_id = raw_task.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise FinalReportInputError(f"{name} task {index} lacks a record identity")
        if record_id in record_ids:
            raise FinalReportInputError(f"{name} record identity is duplicated: {record_id}")
        record_ids.add(record_id)
        patch_sha = str(raw_task.get("patch_sha256") or "").lower()
        if _SHA256_RE.fullmatch(patch_sha) is None:
            raise FinalReportInputError(f"{name} task {index} lacks a full patch SHA-256")
        report_path = raw_task.get("report_path")
        if not isinstance(report_path, str) or not report_path:
            raise FinalReportInputError(f"{name} task {index} lacks an official report path")
        attempt_count = raw_task.get("attempt_count")
        eval_attempt_count = raw_task.get("eval_attempt_count")
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count < 1:
            raise FinalReportInputError(f"{name} task {index} has no accepted attempt")
        if isinstance(eval_attempt_count, bool) or not isinstance(eval_attempt_count, int) or eval_attempt_count < 1:
            raise FinalReportInputError(f"{name} task {index} has no evaluation attempt")
        (resolved if verdict else unresolved).append(index)
        tasks.append(
            {
                "index": index,
                "task": task_id,
                "status": "resolved" if verdict else "unresolved",
                "record_id": record_id,
                "patch_sha256": patch_sha,
                "report_path": report_path,
            }
        )
    if indices != list(expected):
        raise FinalReportInputError(f"{name} task rows must match the exact ordered task census")

    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise FinalReportInputError(f"{name} fact report has no counts object")
    derived_resolved = len(resolved)
    derived_unresolved = len(unresolved)
    required_counts = {
        "tasks": len(expected),
        "eval_success": len(expected),
        "eval_pending": 0,
        "eval_failed": 0,
        "empty_patch": 0,
        "over_budget_tasks": 0,
        "resolved": derived_resolved,
        "unresolved": derived_unresolved,
        "technical_failed_final": 0,
    }
    for key, expected_count in required_counts.items():
        _require_count(counts, key, expected_count, label=f"{name} fact report")
    if set(resolved) & set(unresolved) or set(resolved) | set(unresolved) != set(expected):
        raise FinalReportInputError(f"{name} verdict sets do not partition the task census")
    return MethodFacts(
        name=name,
        source=document,
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
        tasks=tuple(tasks),
    )


def load_audit_manifest(
    path: Path,
    *,
    method: MethodFacts,
    expected: tuple[int, ...],
    dataset_tasks: tuple[DatasetTask, ...],
    expected_dataset_sha256: str,
) -> dict[str, Any]:
    """Bind a clean-run audit manifest to one exact fact report."""

    document = _load_document(path, label=f"{method.name} audit manifest")
    manifest = document.value
    if manifest.get("schema") != AUDIT_MANIFEST_SCHEMA:
        raise FinalReportInputError(f"{method.name} audit manifest has an unsupported schema")
    if manifest.get("method") != method.name:
        raise FinalReportInputError(f"{method.name} audit manifest method does not match")
    if manifest.get("source_report_sha256") != method.source.sha256:
        raise FinalReportInputError(f"{method.name} audit manifest is bound to a different fact report")
    for field in (
        "expected_indices",
        "clean_trajectory_indices",
        "candidate_identity_indices",
        "network_isolation_indices",
        "direct_execution_indices",
    ):
        _require_exact_indices(manifest.get(field), expected, label=f"{method.name} audit {field}")
    _require_exact_indices(
        manifest.get("resolved_execution_indices"),
        method.resolved,
        label=f"{method.name} audit resolved_execution_indices",
    )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise FinalReportInputError(f"{method.name} audit manifest has no runtime object")
    for field in ("opencollab_commit", "opencollab_eval_commit"):
        value = str(runtime.get(field) or "").lower()
        if _COMMIT_RE.fullmatch(value) is None:
            raise FinalReportInputError(f"{method.name} audit runtime {field} is invalid")
    dataset_sha = str(runtime.get("dataset_sha256") or "").lower()
    if dataset_sha != expected_dataset_sha256:
        raise FinalReportInputError(
            f"{method.name} audit runtime dataset_sha256 does not match the trusted dataset"
        )
    normalized_runtime = {
        "opencollab_commit": str(runtime["opencollab_commit"]).lower(),
        "opencollab_eval_commit": str(runtime["opencollab_eval_commit"]).lower(),
        "dataset_sha256": dataset_sha,
    }
    evidence = manifest.get("evidence_files")
    if not isinstance(evidence, list) or not evidence:
        raise FinalReportInputError(f"{method.name} audit manifest has no evidence files")
    facts_by_index = {task["index"]: task for task in method.tasks}
    verified_evidence: list[dict[str, str]] = []
    verified_indices: list[int] = []
    seen_paths: set[str] = set()
    verified_payloads: set[tuple[str, str]] = set()
    official_payload_cache: dict[tuple[str, str], bytes] = {}
    artifact_path_hashes: dict[str, str] = {}
    verified_artifacts: dict[tuple[str, str], dict[str, Any]] = {}
    for position, entry in enumerate(evidence, start=1):
        if not isinstance(entry, dict):
            raise FinalReportInputError(f"{method.name} audit evidence {position} is not an object")
        raw_path = entry.get("path")
        expected_sha = str(entry.get("sha256") or "").lower()
        if not isinstance(raw_path, str) or not raw_path or _SHA256_RE.fullmatch(expected_sha) is None:
            raise FinalReportInputError(f"{method.name} audit evidence {position} is invalid")
        if raw_path in seen_paths:
            raise FinalReportInputError(f"{method.name} audit evidence path is duplicated: {raw_path}")
        seen_paths.add(raw_path)
        evidence_path = Path(raw_path)
        if not evidence_path.is_absolute():
            evidence_path = path.parent / evidence_path
        evidence_document = _load_document(
            evidence_path,
            label=f"{method.name} audit evidence {position}",
        )
        if evidence_document.sha256 != expected_sha:
            raise FinalReportInputError(f"{method.name} audit evidence hash changed: {raw_path}")
        value = evidence_document.value
        if value.get("schema") != AUDIT_EVIDENCE_SCHEMA:
            raise FinalReportInputError(f"{method.name} audit evidence {position} has an unsupported schema")
        if value.get("method") != method.name or value.get("source_report_sha256") != method.source.sha256:
            raise FinalReportInputError(f"{method.name} audit evidence {position} is bound to another run")
        if value.get("runtime") != normalized_runtime:
            raise FinalReportInputError(f"{method.name} audit evidence {position} has a different runtime")
        task_evidence = value.get("tasks")
        if not isinstance(task_evidence, list) or not task_evidence:
            raise FinalReportInputError(f"{method.name} audit evidence {position} has no task evidence")
        document_indices: list[int] = []
        document_resolved: list[int] = []
        for row_position, row in enumerate(task_evidence, start=1):
            if not isinstance(row, dict):
                raise FinalReportInputError(
                    f"{method.name} audit evidence {position} task {row_position} is not an object"
                )
            index = row.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index not in facts_by_index:
                raise FinalReportInputError(f"{method.name} audit evidence {position} has an invalid task index")
            if index in verified_indices or index in document_indices:
                raise FinalReportInputError(f"{method.name} audit evidence duplicates task {index}")
            fact = facts_by_index[index]
            for field in ("task", "record_id", "patch_sha256"):
                if row.get(field) != fact[field]:
                    raise FinalReportInputError(
                        f"{method.name} audit evidence task {index} has a mismatched {field}"
                    )
            for field in (
                "trajectory_clean",
                "candidate_identity_verified",
                "network_isolated",
                "direct_execution_proven",
            ):
                if row.get(field) is not True:
                    raise FinalReportInputError(
                        f"{method.name} audit evidence task {index} does not prove {field}"
                    )
            artifacts = row.get("artifacts")
            if not isinstance(artifacts, dict) or set(artifacts) != _REQUIRED_TASK_ARTIFACTS:
                raise FinalReportInputError(
                    f"{method.name} audit evidence task {index} must bind exactly "
                    + ", ".join(sorted(_REQUIRED_TASK_ARTIFACTS))
                )
            official_report_payload: bytes | None = None
            for kind in sorted(_REQUIRED_TASK_ARTIFACTS):
                artifact_label = f"{method.name} audit evidence task {index} {kind} artifact"
                artifact_path, artifact_sha, artifact_payload = _read_bound_artifact(
                    artifacts[kind],
                    anchor=evidence_document.path.parent,
                    label=artifact_label,
                    verified=verified_payloads,
                    payload_cache=official_payload_cache,
                    retain_payload=kind == "official_report",
                )
                normalized_path = Path(artifact_path)
                if not normalized_path.is_absolute():
                    normalized_path = evidence_document.path.parent / normalized_path
                normalized_key = str(normalized_path)
                previous_sha = artifact_path_hashes.setdefault(normalized_key, artifact_sha)
                if previous_sha != artifact_sha:
                    raise FinalReportInputError(
                        f"{method.name} audit artifact path has conflicting hashes: {artifact_path}"
                    )
                verified = verified_artifacts.setdefault(
                    (normalized_key, artifact_sha),
                    {"path": artifact_path, "sha256": artifact_sha, "kinds": []},
                )
                if kind not in verified["kinds"]:
                    verified["kinds"].append(kind)
                if kind == "official_report":
                    if artifact_path != fact["report_path"]:
                        raise FinalReportInputError(
                            f"{method.name} audit evidence task {index} official report path "
                            "does not match the fact report"
                        )
                    official_report_payload = artifact_payload
            if official_report_payload is None:
                raise FinalReportInputError(
                    f"{method.name} audit evidence task {index} lacks an official report"
                )
            _verify_official_report(
                official_report_payload,
                fact=fact,
                dataset_task=dataset_tasks[index - 1],
                label=f"{method.name} audit evidence task {index} official report",
            )
            document_indices.append(index)
            if fact["status"] == "resolved":
                document_resolved.append(index)
        for field in (
            "covered_indices",
            "clean_trajectory_indices",
            "candidate_identity_indices",
            "network_isolation_indices",
            "direct_execution_indices",
        ):
            _require_exact_indices(
                value.get(field),
                tuple(document_indices),
                label=f"{method.name} audit evidence {position} {field}",
            )
        _require_exact_indices(
            value.get("resolved_execution_indices"),
            tuple(document_resolved),
            label=f"{method.name} audit evidence {position} resolved_execution_indices",
        )
        verified_indices.extend(document_indices)
        verified_evidence.append({"path": raw_path, "sha256": evidence_document.sha256})
    if verified_indices != list(expected):
        raise FinalReportInputError(f"{method.name} audit evidence does not cover the exact ordered task census")
    return {
        "manifest_path": str(path),
        "manifest_sha256": document.sha256,
        "runtime": normalized_runtime,
        "coverage": len(expected),
        "resolved_execution_count": len(method.resolved),
        "evidence_files": verified_evidence,
        "supporting_artifacts": list(verified_artifacts.values()),
    }


def load_optional_document(path: Path | None, *, schema: str, label: str) -> LoadedDocument | None:
    if path is None:
        return None
    document = _load_document(path, label=label)
    if document.value.get("schema") != schema:
        raise FinalReportInputError(f"{label} has an unsupported schema")
    return document


def build_comparison(
    *,
    method_a: MethodFacts,
    method_b: MethodFacts,
    audit_a: dict[str, Any],
    audit_b: dict[str, Any],
    expected: tuple[int, ...],
    dataset: str,
    dataset_source: LoadedDataset,
    author: str,
    meeting_date: str,
    narrative: LoadedDocument | None,
    labels: LoadedDocument | None,
) -> dict[str, Any]:
    """Create the single model consumed by every report renderer."""

    task_ids_a = {task["index"]: task["task"] for task in method_a.tasks}
    task_ids_b = {task["index"]: task["task"] for task in method_b.tasks}
    mismatched_indices = [index for index in expected if task_ids_a[index] != task_ids_b[index]]
    if mismatched_indices:
        raise FinalReportInputError(
            "method fact reports map the same indices to different tasks: "
            + ", ".join(str(index) for index in mismatched_indices)
        )

    resolved_a = set(method_a.resolved)
    resolved_b = set(method_b.resolved)
    common = tuple(index for index in expected if index in resolved_a & resolved_b)
    only_a = tuple(index for index in expected if index in resolved_a - resolved_b)
    only_b = tuple(index for index in expected if index in resolved_b - resolved_a)
    neither = tuple(index for index in expected if index not in (resolved_a | resolved_b))
    statuses_a = {task["index"]: task["status"] for task in method_a.tasks}
    statuses_b = {task["index"]: task["status"] for task in method_b.tasks}
    tasks = [
        {
            "index": index,
            "task": task_ids_a[index],
            "method_a": statuses_a[index],
            "method_b": statuses_b[index],
        }
        for index in expected
    ]
    segments = []
    for start, end in ((1, 25), (26, 50), (51, 75), (76, 100)):
        indices = tuple(index for index in expected if start <= index <= end)
        if not indices:
            continue
        segments.append(
            {
                "start": indices[0],
                "end": indices[-1],
                "method_a": {
                    "resolved": sum(index in resolved_a for index in indices),
                    "unresolved": sum(index not in resolved_a for index in indices),
                },
                "method_b": {
                    "resolved": sum(index in resolved_b for index in indices),
                    "unresolved": sum(index not in resolved_b for index in indices),
                },
            }
        )
    return {
        "schema": COMPARISON_SCHEMA,
        "status": "final",
        "generated_at": meeting_date,
        "author": author,
        "dataset": dataset,
        "generator_version": __version__,
        "methods": {"method_a": method_a.name, "method_b": method_b.name},
        "counts": {
            "tasks": len(expected),
            "method_a": {
                "resolved": len(method_a.resolved),
                "unresolved": len(method_a.unresolved),
                "technical": 0,
                "confirmed_terminal": len(expected),
            },
            "method_b": {
                "resolved": len(method_b.resolved),
                "unresolved": len(method_b.unresolved),
                "technical": 0,
                "confirmed_terminal": len(expected),
            },
        },
        "comparison": {
            "common_resolved_count": len(common),
            "only_method_a_resolved_count": len(only_a),
            "only_method_b_resolved_count": len(only_b),
            "neither_resolved_count": len(neither),
        },
        "indices": {
            "method_a_resolved": list(method_a.resolved),
            "method_a_unresolved": list(method_a.unresolved),
            "method_b_resolved": list(method_b.resolved),
            "method_b_unresolved": list(method_b.unresolved),
            "common_resolved": list(common),
            "only_method_a_resolved": list(only_a),
            "only_method_b_resolved": list(only_b),
            "neither_resolved": list(neither),
        },
        "segments": segments,
        "tasks": tasks,
        "integrity": {
            "dataset": {
                "path": str(dataset_source.path),
                "sha256": dataset_source.sha256,
                "task_count": len(dataset_source.tasks),
            },
            "method_a": {
                "fact_report_path": str(method_a.source.path),
                "fact_report_sha256": method_a.source.sha256,
                **audit_a,
            },
            "method_b": {
                "fact_report_path": str(method_b.source.path),
                "fact_report_sha256": method_b.source.sha256,
                **audit_b,
            },
            "candidate_identity_rule": "Every terminal task has a bound record ID and full patch SHA-256.",
            "pass_rule": "Every resolved task has direct official execution proof.",
            "clean_run_rule": "Every task is covered by trajectory, identity, and network-isolation audit evidence.",
        },
        "narrative": narrative.value if narrative else None,
        "narrative_sha256": narrative.sha256 if narrative else None,
        "labels": labels.value if labels else None,
        "labels_sha256": labels.sha256 if labels else None,
    }


__all__ = [
    "AUDIT_EVIDENCE_SCHEMA",
    "AUDIT_MANIFEST_SCHEMA",
    "COMPARISON_SCHEMA",
    "FACT_REPORT_SCHEMA",
    "FinalReportInputError",
    "LABELS_SCHEMA",
    "NARRATIVE_SCHEMA",
    "build_comparison",
    "load_audit_manifest",
    "load_method_facts",
    "load_optional_document",
]
