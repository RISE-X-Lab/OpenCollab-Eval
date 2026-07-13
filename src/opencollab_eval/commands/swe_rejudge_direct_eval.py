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
from pathlib import Path

from opencollab_eval.engine.swe_eval_records import direct_eval_done_has_execution_proof
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

_COMMAND_PERMISSION_ERROR_RE = re.compile(
    r"^unsafe:(?:f2p|p2p)\.command:(?:PermissionError|UnsafeRecordInputError)$"
)


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


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _matching_attempts(rows: list[dict], source: dict) -> list[dict]:
    identity = {
        "task": source.get("task"),
        "record_id": source.get("record_id"),
        "patch_sha256": source.get("patch_sha256"),
        "eval_spec_sha256": source.get("eval_spec_sha256"),
    }
    return [
        row
        for row in rows
        if row.get("phase") == "eval_attempt_started"
        and all(row.get(key) == value for key, value in identity.items() if value)
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
    commands = plan.get("commands")
    proofs = plan.get("proofs")
    if plan.get("schema") != "opencollab.prolite_test_plan.v2":
        raise RuntimeError(f"{label} plan has an unsupported schema")
    if not isinstance(commands, list) or any(not isinstance(command, str) or not command for command in commands):
        raise RuntimeError(f"{label} plan has invalid commands")
    if require_commands and not commands:
        raise RuntimeError(f"{label} plan has no executable commands")
    if not isinstance(proofs, list) or len(proofs) != len(commands):
        raise RuntimeError(f"{label} plan does not bind one proof to each command")
    if plan.get("adapter") == "pytest" or any(
        isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports"
        for proof in proofs
    ):
        raise RuntimeError(f"{label} plan requires an external Python result boundary")
    if commands and plan.get("coverage_verified") is not True:
        raise RuntimeError(f"{label} plan does not verify target coverage")
    if any(not isinstance(proof, dict) or not proof for proof in proofs):
        raise RuntimeError(f"{label} plan contains an unstructured proof")


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
    allowed_reasons = {
        "unsafe_or_missing_output_artifact",
        "fail_to_pass_evidence",
        "pass_to_pass_evidence",
    }
    if (
        "unsafe_or_missing_output_artifact" not in source_reasons
        or not source_reasons.issubset(allowed_reasons)
    ):
        raise RuntimeError("source technical reasons are broader than aggregate command permissions")
    source_errors = set(source.get("output_artifact_errors") or [])
    if not source_errors or not all(_COMMAND_PERMISSION_ERROR_RE.fullmatch(error) for error in source_errors):
        raise RuntimeError("source output errors are not aggregate command permission failures")

    task = validate_task_identity(source.get("task"))
    input_dir = eval_dir / "input"
    _, f2p_plan = _read_json(input_dir / "f2p.plan.json")
    _, p2p_plan = _read_json(input_dir / "p2p.plan.json")
    _validate_execution_plan(f2p_plan, label="fail-to-pass", require_commands=True)
    _validate_execution_plan(p2p_plan, label="pass-to-pass", require_commands=False)
    proof_nonce = _read_regular_bytes(input_dir / "proof.nonce", limit=256).decode("ascii").strip()
    if not proof_nonce:
        raise RuntimeError("empty proof nonce")
    previous_tests = source.get("tests_status")
    if isinstance(previous_tests, dict):
        for key, plan in (("fail_to_pass_plan", f2p_plan), ("pass_to_pass_plan", p2p_plan)):
            if previous_tests.get(key) != plan:
                raise RuntimeError(f"source summary and input disagree on {key}")

    report_dir = eval_dir / "reports" / task
    artifacts = read_eval_output_artifacts(report_dir, f2p_plan, p2p_plan, proof_nonce)
    if artifacts["output_artifact_errors"]:
        raise RuntimeError(
            "verdict-relevant artifacts remain unsafe: "
            + ", ".join(artifacts["output_artifact_errors"])
        )
    if set(artifacts["diagnostic_artifact_errors"]) != source_errors:
        raise RuntimeError("current aggregate command diagnostics do not match the source failure")

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
    matching = _matching_attempts(attempts, source)
    if not matching:
        raise RuntimeError("no persisted eval attempt matches the source summary identity")

    source_sha = _sha256(source_raw)
    attempts_sha = _sha256(attempts_raw)
    derived = dict(source)
    derived.update(
        {
            "status": verdict["summary_status"],
            "resolved": verdict["resolved"],
            "technical_reasons": verdict["technical_reasons"],
            "output_artifact_errors": artifacts["output_artifact_errors"],
            "diagnostic_artifact_errors": artifacts["diagnostic_artifact_errors"],
            "tests_status": _updated_tests_status(source, artifacts, f2p_plan, p2p_plan),
            "report_path": str(output_dir / "report.json"),
            "rejudgement": {
                "schema": "opencollab.prolite_direct_eval_rejudgement.v1",
                "reason": "aggregate_command_permission_only",
                "source_summary_path": str(source_path),
                "source_summary_sha256": source_sha,
                "eval_attempts_path": str(attempts_path),
                "eval_attempts_sha256": attempts_sha,
                "matching_eval_attempts": len(matching),
                "added_eval_attempts": 0,
                "attempt_identity": {
                    "task": task,
                    "record_id": source.get("record_id"),
                    "patch_sha256": source.get("patch_sha256"),
                    "eval_patch_sha256": source.get("eval_patch_sha256"),
                    "eval_spec_sha256": source.get("eval_spec_sha256"),
                },
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = rejudge(args.eval_dir, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
