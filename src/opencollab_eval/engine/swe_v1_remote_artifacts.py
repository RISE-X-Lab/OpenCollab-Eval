"""Bounded Pro-Lite artifact snapshots and pure verdict derivation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from opencollab_eval.engine.eval_candidate_projection import (
    candidate_projection_valid,
    source_projection_sha256,
    source_projection_valid,
)
from opencollab_eval.engine.swe_generation_proof import (
    preparation_input_valid,
    solver_git_snapshot_valid,
)
from opencollab_eval.engine.swe_test_evidence import target_evidence_passed
from opencollab_eval.engine.swe_v1_remote_commands import (
    _plan_log_failure_proof_matches,
    _plan_log_proof_matches,
)
from opencollab_eval.engine.swe_v1_remote_core import (
    RecordInputFormatError,
    RecordInputLimitError,
    atomic_write_bytes,
)
from opencollab_eval.engine.swe_v1_remote_generation import eval_log_has_infra_failure
from opencollab_eval.engine.swe_v1_remote_records import read_tail_text
from opencollab_eval.engine.swe_v1_remote_state import (
    MAX_EXIT_STATUS_BYTES,
    MAX_LOG_TAIL_BYTES,
    MAX_TEST_EVIDENCE_BYTES,
    open_regular_binary,
)
from opencollab_eval.engine.swe_v1_remote_target_proof import (
    plan_runtime_dependency_specs,
    raw_plan_runtime_dependency_specs,
)

_FIXED_EVAL_OUTPUT_NAMES = (
    "base_commit.exit",
    "base_commit.log",
    "base_snapshot.json",
    "candidate_projection.json",
    "source_candidate_projection.json",
    "runtime_dependencies.json",
    "before_repo.exit",
    "before_repo.log",
    "post_before_base.exit",
    "service_bootstrap.exit",
    "service_bootstrap.log",
    "model_patch.exit",
    "model_patch.log",
    "test_patch.exit",
    "test_patch.log",
    "f2p.command",
    "f2p.exit",
    "f2p.log",
    "p2p.command",
    "p2p.exit",
    "p2p.log",
)


def expected_eval_output_names(f2p_plan, p2p_plan, proof_nonce):
    """Return the exact artifact allowlist consumed by the verdict reader."""
    names = list(_FIXED_EVAL_OUTPUT_NAMES)
    for prefix, plan in (("f2p", f2p_plan), ("p2p", p2p_plan)):
        proofs = plan.get("proofs") or []
        for index, _command in enumerate(plan.get("commands") or [], 1):
            proof = proofs[index - 1] if index <= len(proofs) else None
            stem = f"{prefix}.batch_{index:03d}"
            names.extend((f"{stem}.command", f"{stem}.exit", f"{stem}.log"))
            if isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports":
                names.append(f"{stem}.proof.{proof_nonce}.jsonl")
    return names


def prepare_eval_output_directory(reports_dir, output_dir, task):
    """Create an empty canonical output directory and archive its predecessor."""
    if output_dir.exists() or output_dir.is_symlink():
        archive = reports_dir / ".previous"
        if archive.is_symlink() or (archive.exists() and not archive.is_dir()):
            raise OSError("eval output archive must be a real directory")
        archive.mkdir(exist_ok=True)
        suffix = f"{hashlib.sha256(task.encode()).hexdigest()[:16]}-{uuid.uuid4().hex}"
        output_dir.replace(archive / suffix)
    output_dir.mkdir()
    output_dir.chmod(0o755)


def publish_eval_output_artifacts(source_dir, output_dir, names):
    """Publish only bounded, regular, verdict-relevant artifacts."""
    errors = []
    for name in names:
        try:
            with open_regular_binary(source_dir / name) as handle:
                opened = os.fstat(handle.fileno())
                if opened.st_size > MAX_TEST_EVIDENCE_BYTES:
                    raise RecordInputLimitError(f"eval output exceeds byte limit: {name}")
                payload = handle.read(MAX_TEST_EVIDENCE_BYTES + 1)
            atomic_write_bytes(output_dir / name, payload)
            (output_dir / name).chmod(0o644)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            errors.append(f"publish:{name}:{type(exc).__name__}")
    return errors


def cleanup_temporary_output(temporary_output):
    """Remove the local output tree without masking the evaluation result."""
    try:
        temporary_output.cleanup()
    except OSError as exc:
        return [f"cleanup:temporary_output:{type(exc).__name__}"]
    return []


def create_local_eval_output():
    """Create a Docker-writable output directory on server-local storage."""
    temporary_output = tempfile.TemporaryDirectory(prefix="opencollab-eval-", dir="/tmp")
    output_dir = Path(temporary_output.name)
    output_dir.chmod(0o1777)
    return temporary_output, output_dir


def _read_exit(output_dir, errors, name, default=99):
    path = output_dir / name
    try:
        with open_regular_binary(path) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size > MAX_EXIT_STATUS_BYTES:
                raise RecordInputLimitError(f"exit status exceeds byte limit: {path}")
            raw = handle.read(MAX_EXIT_STATUS_BYTES + 1)
        text = raw.decode("ascii").strip()
        if not re.fullmatch(r"-?[0-9]+", text):
            raise RecordInputFormatError(f"invalid exit status: {path}")
        return int(text)
    except FileNotFoundError:
        errors.append(f"missing:{name}")
        return default
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return default


def _read_text(output_dir, errors, name, limit=4000):
    try:
        return read_tail_text(output_dir / name, limit)
    except OSError as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return ""


def _read_required_text(output_dir, errors, name, limit=4000):
    return _read_required_bytes(output_dir, errors, name, limit).decode(
        "utf-8", errors="replace"
    )


def _read_required_bytes(output_dir, errors, name, limit=4000):
    path = output_dir / name
    try:
        with open_regular_binary(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > limit:
                raise RecordInputLimitError(
                    f"required output artifact exceeds byte limit: {path}"
                )
            return handle.read(limit + 1)
    except FileNotFoundError:
        errors.append(f"missing:{name}")
        return b""
    except (OSError, ValueError) as exc:
        errors.append(f"unsafe:{name}:{type(exc).__name__}")
        return b""


def _read_integrity_report(output_dir, errors, expected_base_commit=""):
    text = _read_required_text(
        output_dir,
        errors,
        "base_snapshot.json",
        MAX_TEST_EVIDENCE_BYTES,
    )
    try:
        report = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        errors.append("unsafe:base_snapshot.json:invalid_json")
        return {}
    pre = report.get("preparation_input_snapshot") if isinstance(report, dict) else None
    post = (
        {key: value for key, value in report.items() if key != "preparation_input_snapshot"}
        if isinstance(report, dict)
        else None
    )
    if (
        not preparation_input_valid(pre)
        or not solver_git_snapshot_valid(post)
        or post.get("expected_base_commit") != pre.get("expected_base_commit")
        or expected_base_commit
        and pre.get("expected_base_commit") != str(expected_base_commit).strip().lower()
    ):
        errors.append("unsafe:base_snapshot.json:invalid_integrity")
        return {}
    return report


def _read_candidate_projection(output_dir, errors, expectation, base_snapshot):
    text = _read_required_text(
        output_dir,
        errors,
        "candidate_projection.json",
        MAX_TEST_EVIDENCE_BYTES,
    )
    try:
        report = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        errors.append("unsafe:candidate_projection.json:invalid_json")
        return {}, {}
    preparation = (
        base_snapshot.get("preparation_input_snapshot")
        if isinstance(base_snapshot, dict)
        else None
    )
    source_projection = {}
    if isinstance(report, dict) and report.get("schema") == "opencollab.eval_candidate_projection.v2":
        raw_source = _read_required_bytes(
            output_dir, errors, "source_candidate_projection.json", MAX_TEST_EVIDENCE_BYTES
        )
        try:
            source_projection = json.loads(raw_source)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("unsafe:source_candidate_projection.json:invalid_json")
            source_projection = {}
        if (
            not source_projection_valid(source_projection, expectation)
            or report.get("source_projection_sha256")
            != source_projection_sha256(source_projection)
        ):
            errors.append("unsafe:source_candidate_projection.json:invalid_integrity")
            source_projection = {}
    common_valid = candidate_projection_valid(report, expectation, source_projection)
    if isinstance(report, dict) and report.get("schema") == "opencollab.eval_candidate_projection.v1":
        valid = bool(
            common_valid
            and isinstance(base_snapshot, dict)
            and report.get("base_commit") == base_snapshot.get("anonymous_head")
            and report.get("base_tree") == base_snapshot.get("base_tree")
            and (
                not report.get("source_base_commit")
                or report.get("source_base_commit") == base_snapshot.get("expected_base_commit")
            )
        )
    else:
        valid = bool(
            common_valid
            and isinstance(preparation, dict)
            and report.get("verified_source_base_commit") == preparation.get("expected_base_commit")
            and report.get("verified_source_base_tree") == preparation.get("base_tree")
            and isinstance(base_snapshot, dict)
            and report.get("prepared_base_commit") == base_snapshot.get("anonymous_head")
            and report.get("prepared_base_tree") == base_snapshot.get("base_tree")
        )
    if not valid:
        errors.append("unsafe:candidate_projection.json:invalid_integrity")
        return {}, {}
    return report, source_projection


def _read_runtime_dependencies(
    output_dir,
    errors,
    f2p_plan,
    p2p_plan,
    identities=None,
    expected_image_id="",
):
    text = _read_required_text(
        output_dir, errors, "runtime_dependencies.json", MAX_TEST_EVIDENCE_BYTES
    )
    try:
        report = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        errors.append("unsafe:runtime_dependencies.json:invalid_json")
        return {}
    if not isinstance(report, dict):
        errors.append("unsafe:runtime_dependencies.json:invalid_integrity")
        return {}
    entries = report.get("entries")
    raw_expected = raw_plan_runtime_dependency_specs(f2p_plan, p2p_plan)
    expected = plan_runtime_dependency_specs(f2p_plan, p2p_plan)
    expected_sha256 = hashlib.sha256(
        json.dumps(raw_expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected_roots = {}
    for item in expected:
        expected_roots[item["root"]] = item
    legacy_roots = {
        item.get("root")
        for item in raw_expected
        if isinstance(item, dict) and set(item) == {"root", "required_paths"}
    }
    expected_file_roots = {
        item["root"] for item in expected if item.get("kind") == "file"
    }
    identity_entries = identities.get("entries") if isinstance(identities, dict) else None
    identity_valid = (
        not expected_file_roots
        and identities is None
        or isinstance(identities, dict)
        and identities.get("schema") == "opencollab.runtime_dependency_identities.v1"
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(identities.get("image_id") or ""))
        is not None
        and (not expected_image_id or identities.get("image_id") == expected_image_id)
        and isinstance(identity_entries, list)
        and len(identity_entries) <= 16
        and all(
            isinstance(item, dict)
            and set(item) == {"root", "content_sha256"}
            and item.get("root") in expected_file_roots
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_sha256") or ""))
            is not None
            for item in identity_entries
        )
        and len({item["root"] for item in identity_entries}) == len(identity_entries)
    )
    identity_hashes = (
        {item["root"]: item["content_sha256"] for item in identity_entries}
        if identity_valid and isinstance(identity_entries, list)
        else {}
    )

    def safe_relative_path(value):
        if not isinstance(value, str) or not value:
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and ".." not in path.parts

    def valid_entry(item):
        if not isinstance(item, dict):
            return False
        legacy = set(item) == {"root", "required_paths"} and item.get("root") in legacy_roots
        if not legacy and set(item) != {
            "root",
            "required_paths",
            "kind",
            "candidate_protected",
            "content_sha256",
        }:
            return False
        root = item.get("root")
        required = item.get("required_paths")
        expected_item = expected_roots.get(root)
        kind = "directory" if legacy else item.get("kind")
        content_sha256 = "" if legacy else item.get("content_sha256")
        candidate_protected = True if legacy else item.get("candidate_protected")
        return (
            safe_relative_path(root)
            and isinstance(expected_item, dict)
            and isinstance(required, list)
            and bool(required)
            and len(required) <= 16
            and all(safe_relative_path(path) for path in required)
            and len(set(required)) == len(required)
            and set(required).issubset(expected_item["required_paths"])
            and kind == expected_item["kind"]
            and candidate_protected == expected_item["candidate_protected"]
            and isinstance(content_sha256, str)
            and (
                not content_sha256
                if kind == "directory"
                else identity_hashes.get(root) == content_sha256
            )
        )

    valid = (
        report.get("schema") == "opencollab.eval_runtime_dependencies.v1"
        and report.get("phase") == "restored"
        and report.get("source") == "pinned_image_runtime_with_trusted_public_preparation"
        and report.get("solver_visible") is False
        and report.get("spec_sha256") == expected_sha256
        and isinstance(entries, list)
        and len(entries) <= 16
        and identity_valid
        and all(valid_entry(item) for item in entries)
        and len({item.get("root") for item in entries if isinstance(item, dict)}) == len(entries)
        and {
            item.get("root")
            for item in entries
            if isinstance(item, dict)
            and (item.get("kind") == "file" or item.get("root") in expected_file_roots)
        }
        == set(identity_hashes)
    )
    if not valid:
        errors.append("unsafe:runtime_dependencies.json:invalid_integrity")
        return {}
    return report


def _read_plan_evidence(output_dir, errors, prefix, plan, proof_nonce):
    evidence = []
    for index, expected_command in enumerate(plan["commands"], 1):
        stem = f"{prefix}.batch_{index:03d}"
        error_count = len(errors)
        status = _read_exit(output_dir, errors, f"{stem}.exit")
        observed_command = _read_text(
            output_dir,
            errors,
            f"{stem}.command",
            MAX_LOG_TAIL_BYTES,
        ).rstrip("\n")
        log_error_count = len(errors)
        log_text = _read_required_text(
            output_dir,
            errors,
            f"{stem}.log",
            MAX_TEST_EVIDENCE_BYTES,
        )
        log_artifact_safe = len(errors) == log_error_count
        proofs = plan.get("proofs") or []
        proof = proofs[index - 1] if index <= len(proofs) else None
        proof_text = ""
        if isinstance(proof, dict) and proof.get("kind") == "pytest_structured_reports":
            proof_text = _read_required_text(
                output_dir,
                errors,
                f"{stem}.proof.{proof_nonce}.jsonl",
                MAX_TEST_EVIDENCE_BYTES,
            )
        evidence.append(
            {
                "batch": index,
                "status": status,
                "command_matches_plan": observed_command == expected_command,
                "log_artifact_safe": log_artifact_safe,
                "target_proof_matches_plan": _plan_log_proof_matches(
                    proof,
                    log_text,
                    proof_text,
                ),
                "target_failure_proof_matches_plan": _plan_log_failure_proof_matches(
                    proof,
                    log_text,
                    proof_text,
                    expected_command,
                    observed_command,
                ),
                "artifact_safe": len(errors) == error_count,
            }
        )
    return evidence


def _plan_evidence_complete(plan, evidence):
    return (
        bool(plan["commands"])
        and len(evidence) == len(plan["commands"])
        and all(
            item["command_matches_plan"]
            and item["log_artifact_safe"]
            and item["target_proof_matches_plan"]
            and item["artifact_safe"]
            for item in evidence
        )
    )


def _plan_execution_evidence_complete(plan, evidence):
    return (
        bool(plan["commands"])
        and len(evidence) == len(plan["commands"])
        and all(
            item["command_matches_plan"]
            and item["log_artifact_safe"]
            and item["artifact_safe"]
            and target_evidence_passed(item) is not None
            for item in evidence
        )
    )


def read_eval_output_artifacts(
    output_dir,
    f2p_plan,
    p2p_plan,
    proof_nonce,
    expected_base_commit="",
    runtime_dependency_identities=None,
    expected_eval_image_id="",
    candidate_expectation=None,
):
    """Read every verdict-relevant artifact before returning one snapshot."""
    errors = []
    diagnostic_errors = []
    base_commit_status = _read_exit(output_dir, errors, "base_commit.exit")
    service_status = _read_exit(output_dir, errors, "service_bootstrap.exit", 0)
    before_status = _read_exit(output_dir, errors, "before_repo.exit")
    post_before_base_status = _read_exit(
        output_dir,
        errors,
        "post_before_base.exit",
    )
    model_status = _read_exit(output_dir, errors, "model_patch.exit")
    test_status = _read_exit(output_dir, errors, "test_patch.exit")
    f2p_status = _read_exit(output_dir, errors, "f2p.exit")
    p2p_status = _read_exit(output_dir, errors, "p2p.exit", 0)
    f2p_log_tail = _read_text(output_dir, errors, "f2p.log")
    p2p_log_tail = _read_text(output_dir, errors, "p2p.log")
    base_snapshot = _read_integrity_report(output_dir, errors, expected_base_commit)
    candidate_projection, source_candidate_projection = _read_candidate_projection(
        output_dir,
        errors,
        candidate_expectation,
        base_snapshot,
    )
    runtime_dependencies = _read_runtime_dependencies(
        output_dir,
        errors,
        f2p_plan,
        p2p_plan,
        runtime_dependency_identities,
        expected_eval_image_id,
    )
    f2p_evidence = _read_plan_evidence(
        output_dir,
        errors,
        "f2p",
        f2p_plan,
        proof_nonce,
    )
    p2p_evidence = (
        _read_plan_evidence(
            output_dir,
            errors,
            "p2p",
            p2p_plan,
            proof_nonce,
        )
        if p2p_plan["commands"]
        else []
    )
    return {
        "output_artifact_errors": errors,
        "base_commit_status": base_commit_status,
        "base_snapshot": base_snapshot,
        "candidate_projection": candidate_projection,
        "source_candidate_projection": source_candidate_projection,
        "runtime_dependencies": runtime_dependencies,
        "service_status": service_status,
        "before_status": before_status,
        "post_before_base_status": post_before_base_status,
        "model_status": model_status,
        "test_status": test_status,
        "f2p_status": f2p_status,
        "p2p_status": p2p_status,
        "f2p_log_tail": f2p_log_tail,
        "p2p_log_tail": p2p_log_tail,
        "f2p_evidence": f2p_evidence,
        "p2p_evidence": p2p_evidence,
        "f2p_evidence_complete": _plan_evidence_complete(f2p_plan, f2p_evidence),
        "p2p_evidence_complete": not p2p_plan["commands"]
        or _plan_evidence_complete(p2p_plan, p2p_evidence),
        "f2p_execution_evidence_complete": _plan_execution_evidence_complete(
            f2p_plan,
            f2p_evidence,
        ),
        "p2p_execution_evidence_complete": not p2p_plan["commands"]
        or _plan_execution_evidence_complete(p2p_plan, p2p_evidence),
        "f2p_command": _read_text(
            output_dir,
            diagnostic_errors,
            "f2p.command",
            1000,
        ),
        "p2p_command": _read_text(
            output_dir,
            diagnostic_errors,
            "p2p.command",
            1000,
        ),
        "diagnostic_artifact_errors": diagnostic_errors,
        "service_bootstrap_log_tail": _read_text(
            output_dir,
            errors,
            "service_bootstrap.log",
        ),
        "base_commit_log_tail": _read_text(output_dir, errors, "base_commit.log"),
        "before_repo_log_tail": _read_text(output_dir, errors, "before_repo.log"),
        "model_patch_log_tail": _read_text(output_dir, errors, "model_patch.log"),
        "test_patch_log_tail": _read_text(output_dir, errors, "test_patch.log"),
    }


def publish_and_read_eval_output_artifacts(
    source_dir,
    output_dir,
    f2p_plan,
    p2p_plan,
    proof_nonce,
    temporary_output,
    expected_base_commit="",
    runtime_dependency_identities=None,
    expected_eval_image_id="",
    candidate_expectation=None,
):
    """Publish the allowlist and rebuild the verdict snapshot from durable files."""
    publish_errors = publish_eval_output_artifacts(
        source_dir,
        output_dir,
        expected_eval_output_names(f2p_plan, p2p_plan, proof_nonce),
    )
    cleanup_errors = cleanup_temporary_output(temporary_output)
    artifacts = read_eval_output_artifacts(
        output_dir,
        f2p_plan,
        p2p_plan,
        proof_nonce,
        expected_base_commit,
        runtime_dependency_identities,
        expected_eval_image_id,
        candidate_expectation,
    )
    artifacts["output_artifact_errors"] = (
        publish_errors + cleanup_errors + artifacts["output_artifact_errors"]
    )
    return artifacts


def _plan_evidence_mismatch(artifacts, prefix):
    evidence = artifacts[f"{prefix}_evidence"]
    status = artifacts[f"{prefix}_status"]
    aggregate_status = next(
        (item["status"] for item in evidence if item["status"] != 0),
        0,
    )
    return not artifacts[f"{prefix}_execution_evidence_complete"] or bool(
        evidence and aggregate_status != status
    )


def derive_eval_verdict(artifacts, *, docker_exit, cleanup_quiesced, container_cleanup):
    """Derive a verdict only from an already complete artifact snapshot."""
    f2p_evidence = artifacts["f2p_evidence"]
    p2p_evidence = artifacts["p2p_evidence"]
    f2p_status = artifacts["f2p_status"]
    p2p_status = artifacts["p2p_status"]
    reason_checks = (
        ("unsafe_or_missing_output_artifact", bool(artifacts["output_artifact_errors"])),
        ("fail_to_pass_evidence", _plan_evidence_mismatch(artifacts, "f2p")),
        ("pass_to_pass_evidence", _plan_evidence_mismatch(artifacts, "p2p")),
        ("docker_exit", docker_exit != 0),
        ("process_cleanup", not cleanup_quiesced),
        ("container_cleanup", not container_cleanup.get("ok")),
        ("base_commit", artifacts["base_commit_status"] != 0),
        ("base_snapshot_integrity", not artifacts["base_snapshot"]),
        ("candidate_projection", not artifacts["candidate_projection"]),
        ("service_bootstrap", artifacts["service_status"] != 0),
        ("before_repo", artifacts["before_status"] != 0),
        (
            "post_before_base_commit",
            artifacts["post_before_base_status"] != 0,
        ),
        ("model_patch", artifacts["model_status"] != 0),
        ("test_patch", artifacts["test_status"] != 0),
        (
            "fail_to_pass_infra",
            eval_log_has_infra_failure(f2p_status, artifacts["f2p_log_tail"]),
        ),
        (
            "pass_to_pass_infra",
            eval_log_has_infra_failure(p2p_status, artifacts["p2p_log_tail"]),
        ),
    )
    technical_reasons = [reason for reason, active in reason_checks if active]
    technical_error = bool(technical_reasons)
    resolved = bool(
        not technical_error
        and all(target_evidence_passed(item) is True for item in f2p_evidence)
        and all(target_evidence_passed(item) is True for item in p2p_evidence)
    )
    return {
        "technical_reasons": technical_reasons,
        "technical_error": technical_error,
        "resolved": resolved,
        "summary_status": "technical_eval_failed" if technical_error else "done",
    }


__all__ = [
    "cleanup_temporary_output",
    "create_local_eval_output",
    "derive_eval_verdict",
    "expected_eval_output_names",
    "prepare_eval_output_directory",
    "publish_and_read_eval_output_artifacts",
    "publish_eval_output_artifacts",
    "read_eval_output_artifacts",
]
