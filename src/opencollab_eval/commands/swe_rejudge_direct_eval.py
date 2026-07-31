#!/usr/bin/env python3
"""Derive a new verdict from immutable direct-eval artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

from opencollab_eval.commands import _swe_eval_layer_integrity
from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof
from opencollab_eval.engine.swe_test_plan_contract import validated_test_plan_kind
from opencollab_eval.engine.swe_v1_remote_artifacts import (
    derive_eval_verdict,
    read_eval_output_artifacts,
)
from opencollab_eval.engine.swe_v1_remote_records import validate_task_identity
from opencollab_eval.engine.swe_v1_remote_state import (
    MAX_JSON_DOCUMENT_BYTES,
    MAX_JSONL_SCAN_BYTES,
    open_regular_binary,
)
from opencollab_eval.engine.swe_v1_remote_target_proof import plan_runtime_dependency_specs

_COMMAND_PERMISSION_ERROR_RE = re.compile(
    r"^unsafe:(?:f2p|p2p)\.command:(?:PermissionError|UnsafeRecordInputError)$"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _read_regular_bytes(path: Path, *, limit: int) -> bytes:
    with open_regular_binary(path) as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > limit:
            raise RuntimeError(f"artifact exceeds {limit} bytes: {path}")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"artifact exceeds {limit} bytes: {path}")
    return data


def _read_json(path: Path) -> tuple[bytes, dict]:
    raw = _read_regular_bytes(path, limit=MAX_JSON_DOCUMENT_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON artifact must be an object: {path}")
    return raw, value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_attempts(path: Path) -> tuple[bytes, list[dict]]:
    raw = _read_regular_bytes(path, limit=MAX_JSONL_SCAN_BYTES)
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise RuntimeError(f"blank eval-attempt record at line {number}: {path}")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid eval-attempt record at line {number}: {path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"eval-attempt record must be an object at line {number}: {path}")
        rows.append(value)
    return raw, rows


def _write_exclusive(path: Path, data: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError(f"zero-byte write: {path}")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_exclusive_after(
    path: Path,
    data: bytes,
    unchanged: tuple[tuple[Path, bytes], ...],
) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o644)
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError(f"zero-byte write: {temporary}")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        for source_path, expected in unchanged:
            if _read_regular_bytes(source_path, limit=MAX_JSON_DOCUMENT_BYTES) != expected:
                raise RuntimeError(f"source artifact changed during reconciliation: {source_path}")
        os.link(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _source_attempt_identity(source: dict, task: str) -> dict[str, str]:
    identity = {
        "task": task,
        "record_id": source.get("record_id"),
        "patch_sha256": source.get("patch_sha256"),
        "eval_patch_sha256": source.get("eval_patch_sha256"),
        "eval_spec_sha256": source.get("eval_spec_sha256"),
        "eval_image_id": source.get("eval_image_id"),
    }
    record_id = identity["record_id"]
    if (
        not isinstance(record_id, str)
        or not record_id
        or len(record_id.encode("utf-8")) > 256
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in record_id)
    ):
        raise RuntimeError("source summary has an invalid or missing record_id")
    for key in ("patch_sha256", "eval_patch_sha256", "eval_spec_sha256"):
        value = identity[key]
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise RuntimeError(f"source summary has an invalid or missing {key}")
    image_id = identity["eval_image_id"]
    if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
        raise RuntimeError("source summary has an invalid or missing eval_image_id")
    return {key: str(value) for key, value in identity.items()}


def _matching_attempts(rows: list[dict], identity: dict[str, str]) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("phase") == "eval_attempt_started"
        and all(row.get(key) == value for key, value in identity.items())
    ]


def _updated_tests_status(source: dict, artifacts: dict, f2p_plan: dict, p2p_plan: dict) -> dict:
    previous = source.get("tests_status")
    tests = dict(previous) if isinstance(previous, dict) else {}
    tests.update(
        {
            "base_commit_status": artifacts["base_commit_status"],
            "service_bootstrap_status": artifacts["service_status"],
            "before_repo_status": artifacts["before_status"],
            "post_before_base_status": artifacts["post_before_base_status"],
            "model_patch_status": artifacts["model_status"],
            "test_patch_status": artifacts["test_status"],
            "fail_to_pass_status": artifacts["f2p_status"],
            "pass_to_pass_status": artifacts["p2p_status"],
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": artifacts["f2p_evidence"],
            "pass_to_pass_evidence": artifacts["p2p_evidence"],
            "f2p_command": artifacts["f2p_command"],
            "p2p_command": artifacts["p2p_command"],
            "base_commit_log_tail": artifacts["base_commit_log_tail"],
            "before_repo_log_tail": artifacts["before_repo_log_tail"],
            "service_bootstrap_log_tail": artifacts["service_bootstrap_log_tail"],
            "model_patch_log_tail": artifacts["model_patch_log_tail"],
            "test_patch_log_tail": artifacts["test_patch_log_tail"],
            "f2p_log_tail": artifacts["f2p_log_tail"],
            "p2p_log_tail": artifacts["p2p_log_tail"],
        }
    )
    return tests


def _validate_execution_plan(plan: dict, *, label: str, require_commands: bool) -> None:
    if validated_test_plan_kind(plan, require_commands=require_commands) is None:
        raise RuntimeError(f"{label} plan does not satisfy the executable plan contract")


def rejudge(eval_dir: Path, output_dir: Path) -> dict:
    eval_dir = eval_dir.resolve()
    output_dir = output_dir.absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise RuntimeError(f"derived output already exists: {output_dir}")

    source_path = eval_dir / "summary.json"
    source_raw, source = _read_json(source_path)
    if source.get("status") != "technical_eval_failed":
        raise RuntimeError("source summary is not a technical failure")
    source_reasons = set(source.get("technical_reasons") or [])
    evidence_reasons = {"fail_to_pass_evidence", "pass_to_pass_evidence"}
    allowed_reasons = {"unsafe_or_missing_output_artifact", *evidence_reasons}
    permission_reclassification = (
        "unsafe_or_missing_output_artifact" in source_reasons
        and source_reasons.issubset(allowed_reasons)
    )
    evidence_reclassification = (
        bool(source_reasons)
        and source_reasons.issubset(evidence_reasons)
    )
    if not (permission_reclassification or evidence_reclassification):
        raise RuntimeError("source technical reasons are not eligible for artifact rejudgement")
    source_errors = set(source.get("output_artifact_errors") or [])
    source_diagnostic_errors = set(source.get("diagnostic_artifact_errors") or [])
    if permission_reclassification and (
        not source_errors
        or not all(_COMMAND_PERMISSION_ERROR_RE.fullmatch(error) for error in source_errors)
    ):
        raise RuntimeError("source output errors are not aggregate command permission failures")
    if evidence_reclassification and (source_errors or source_diagnostic_errors):
        raise RuntimeError("evidence-only rejudgement cannot carry artifact errors")

    task = validate_task_identity(source.get("task"))
    input_dir = eval_dir / "input"
    _, f2p_plan = _read_json(input_dir / "f2p.plan.json")
    _, p2p_plan = _read_json(input_dir / "p2p.plan.json")
    runtime_identity_path = input_dir / "runtime_dependency_identities.json"
    if runtime_identity_path.exists():
        _, runtime_dependency_identities = _read_json(runtime_identity_path)
    elif any(
        item.get("kind") == "file"
        for item in plan_runtime_dependency_specs(f2p_plan, p2p_plan)
    ):
        raise RuntimeError("runtime dependency identity evidence is missing")
    else:
        runtime_dependency_identities = None
    _validate_execution_plan(f2p_plan, label="fail-to-pass", require_commands=True)
    _validate_execution_plan(p2p_plan, label="pass-to-pass", require_commands=False)
    proof_nonce = _read_regular_bytes(input_dir / "proof.nonce", limit=256).decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{32}", proof_nonce) is None:
        raise RuntimeError("invalid proof nonce")
    previous_tests = source.get("tests_status")
    if not isinstance(previous_tests, dict):
        raise RuntimeError("source summary lacks tests_status plan bindings")
    for key, plan in (("fail_to_pass_plan", f2p_plan), ("pass_to_pass_plan", p2p_plan)):
        if previous_tests.get(key) != plan:
            raise RuntimeError(f"source summary and input disagree on {key}")

    report_dir = eval_dir / "reports" / task
    artifacts = read_eval_output_artifacts(
        report_dir,
        f2p_plan,
        p2p_plan,
        proof_nonce,
        runtime_dependency_identities=runtime_dependency_identities,
        expected_eval_image_id=str(source.get("eval_image_id") or ""),
        candidate_expectation=source.get("candidate_expectation"),
    )
    if artifacts["output_artifact_errors"]:
        raise RuntimeError(
            "verdict-relevant artifacts remain unsafe: "
            + ", ".join(artifacts["output_artifact_errors"])
        )
    expected_diagnostic_errors = (
        source_errors if permission_reclassification else source_diagnostic_errors
    )
    if set(artifacts["diagnostic_artifact_errors"]) != expected_diagnostic_errors:
        raise RuntimeError("current artifact diagnostics do not match the source failure")

    verdict = derive_eval_verdict(
        artifacts,
        docker_exit=int(source.get("docker_exit")),
        cleanup_quiesced=source.get("cleanup_quiesced") is True,
        container_cleanup=source.get("container_cleanup") or {},
    )
    if verdict["technical_error"]:
        raise RuntimeError(
            "recomputed artifacts still have technical failures: "
            + ", ".join(verdict["technical_reasons"])
        )

    attempts_path = eval_dir.parent / "eval_attempts.jsonl"
    attempts_raw, attempts = _read_attempts(attempts_path)
    attempt_identity = _source_attempt_identity(source, task)
    matching = _matching_attempts(attempts, attempt_identity)
    if not matching:
        raise RuntimeError("no persisted eval attempt matches the source summary identity")

    source_sha = _sha256(source_raw)
    attempts_sha = _sha256(attempts_raw)
    derived = dict(source)
    derived.update(
        {
            "status": verdict["summary_status"],
            "outcome": verdict["outcome"],
            "outcome_basis": verdict["outcome_basis"],
            "resolved": verdict["resolved"],
            "technical_reasons": verdict["technical_reasons"],
            "operational_warnings": verdict["operational_warnings"],
            "output_artifact_errors": artifacts["output_artifact_errors"],
                "diagnostic_artifact_errors": artifacts["diagnostic_artifact_errors"],
                "candidate_projection": artifacts["candidate_projection"],
                "source_candidate_projection": artifacts["source_candidate_projection"],
                "tests_status": _updated_tests_status(source, artifacts, f2p_plan, p2p_plan),
            "report_path": str(output_dir / "report.json"),
            "rejudgement": {
                "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
                "reason": (
                    "aggregate_command_permission_only"
                    if permission_reclassification
                    else "structured_test_evidence_only"
                ),
                "source_summary_path": str(source_path),
                "source_summary_sha256": source_sha,
                "eval_attempts_path": str(attempts_path),
                "eval_attempts_sha256": attempts_sha,
                "matching_eval_attempts": len(matching),
                "added_eval_attempts": 0,
                "attempt_identity": attempt_identity,
            },
        }
    )
    if not direct_eval_done_has_execution_proof(
        derived,
        expected_eval_spec_sha256=str(source.get("eval_spec_sha256") or ""),
        expected_f2p_plan=f2p_plan,
        expected_p2p_plan=p2p_plan,
    ):
        raise RuntimeError("derived report lacks complete executable target-test proof")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        _write_exclusive(temporary / "source_summary.json", source_raw, 0o444)
        _write_exclusive(temporary / "summary.json", _json_bytes(derived))
        _write_exclusive(temporary / "report.json", _json_bytes({task: {**derived, "instance_id": task}}))
        if _sha256(_read_regular_bytes(source_path, limit=MAX_JSON_DOCUMENT_BYTES)) != source_sha:
            raise RuntimeError("source summary changed during rejudgement")
        if _sha256(_read_regular_bytes(attempts_path, limit=MAX_JSONL_SCAN_BYTES)) != attempts_sha:
            raise RuntimeError("eval attempts changed during rejudgement")
        os.rename(temporary, output_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return derived


def reconcile_launcher_report(
    launcher_report: Path,
    derived_output_dir: Path,
    output_path: Path,
) -> dict:
    """Publish one eval-only row that binds a derived verdict to its launcher."""
    launcher_raw, launcher = _read_json(launcher_report)
    derived_raw, derived = _read_json(derived_output_dir / "summary.json")
    source_raw, source = _read_json(derived_output_dir / "source_summary.json")
    rows = launcher.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("launcher report must contain exactly one task row")
    row = rows[0]
    evaluation = row.get("eval")
    generation = row.get("generation")
    if not isinstance(evaluation, dict) or evaluation.get("summary") != source:
        raise RuntimeError("launcher evaluation does not bind the rejudged source summary")
    if not isinstance(generation, dict):
        raise RuntimeError("launcher report lacks generation identity")
    task = validate_task_identity(derived.get("task"))
    expected = {
        "task": task,
        "record_id": derived.get("record_id"),
        "source_patch_sha256": derived.get("source_patch_sha256"),
        "eval_patch_sha256": derived.get("eval_patch_sha256"),
    }
    observed = {
        "task": row.get("task"),
        "record_id": generation.get("record_id"),
        "source_patch_sha256": generation.get("source_patch_sha256"),
        "eval_patch_sha256": generation.get("eval_patch_sha256"),
    }
    rejudgement = derived.get("rejudgement")
    attempt_identity = (
        rejudgement.get("attempt_identity") if isinstance(rejudgement, dict) else None
    )
    expected_attempt_identity = {
        "task": task,
        "record_id": derived.get("record_id"),
        "patch_sha256": derived.get("source_patch_sha256"),
        "eval_patch_sha256": derived.get("eval_patch_sha256"),
        "eval_spec_sha256": derived.get("eval_spec_sha256"),
        "eval_image_id": derived.get("eval_image_id"),
    }
    matching_attempts = (
        rejudgement.get("matching_eval_attempts") if isinstance(rejudgement, dict) else None
    )
    if (
        expected != observed
        or derived.get("schema") != "opencollab.prolite_direct_eval.v2"
        or not isinstance(rejudgement, dict)
        or rejudgement.get("schema") != "opencollab.prolite_direct_eval_rejudgement.v1"
        or rejudgement.get("source_summary_sha256") != _sha256(source_raw)
        or rejudgement.get("added_eval_attempts") != 0
        or isinstance(matching_attempts, bool)
        or not isinstance(matching_attempts, int)
        or matching_attempts < 1
        or attempt_identity != expected_attempt_identity
        or not isinstance(derived.get("eval_spec_sha256"), str)
        or _SHA256_RE.fullmatch(derived["eval_spec_sha256"]) is None
        or not isinstance(derived.get("eval_image_id"), str)
        or _IMAGE_ID_RE.fullmatch(derived["eval_image_id"]) is None
        or not direct_eval_done_has_execution_proof(derived)
    ):
        raise RuntimeError("derived verdict does not bind the launcher candidate")
    reconciled = json.loads(launcher_raw)
    reconciled_row = reconciled["rows"][0]
    reconciled_row["eval"].update(
        status="eval_done",
        summary=derived,
        report_path=str(derived_output_dir / "report.json"),
        executed=False,
        eval_patch_sha256=derived["eval_patch_sha256"],
    )
    integrity = _swe_eval_layer_integrity.attempt_integrity(reconciled_row, task)
    if integrity.reasons or not integrity.direct_execution_proven:
        raise RuntimeError("reconciled launcher report lacks complete task evidence")
    reconciled.update(
        status="done",
        rejudgement={
            "schema": "opencollab.eval_only_reconciliation.v1",
            "launcher_report": str(launcher_report),
            "launcher_report_sha256": _sha256(launcher_raw),
            "derived_summary": str(derived_output_dir / "summary.json"),
            "derived_summary_sha256": _sha256(derived_raw),
        },
    )
    _publish_exclusive_after(
        output_path,
        _json_bytes(reconciled),
        (
            (launcher_report, launcher_raw),
            (derived_output_dir / "summary.json", derived_raw),
            (derived_output_dir / "source_summary.json", source_raw),
        ),
    )
    return reconciled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--launcher-report", type=Path)
    parser.add_argument("--reconciliation-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.launcher_report) != bool(args.reconciliation_output):
        raise SystemExit("--launcher-report and --reconciliation-output must be used together")
    result = rejudge(args.eval_dir, args.output_dir)
    if args.launcher_report is not None:
        reconciled = reconcile_launcher_report(
            args.launcher_report,
            args.output_dir,
            args.reconciliation_output,
        )
        result["reconciliation_output"] = str(args.reconciliation_output)
        result["reconciliation_status"] = reconciled["status"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
