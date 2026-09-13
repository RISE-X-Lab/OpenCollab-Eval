"""Retain an owned workflow container when trusted extraction did not finish."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path


def _owned(gp, run_dir: Path, cid: str, name: str):
    path = gp.container_owner_path(run_dir, name)
    record = gp._read_owner(path)
    if record is None or record.get("container_id") != cid or record.get("container_name") != name:
        raise RuntimeError("candidate retention ownership record does not match container")
    if gp._container_owner_label_state(cid, record["owner_token"]) != "matching":
        raise RuntimeError("candidate retention Docker owner label does not match")
    return path, record


def arm_candidate_retention(gp, *, run_dir: Path, cid: str, name: str) -> None:
    """Persist protection before the workflow can create its only candidate."""
    _owned(gp, run_dir, cid, name)
    gp.mark_container_kept(run_dir, cid)


def complete_candidate_retention(gp, *, run_dir: Path, cid: str, name: str) -> None:
    """Return a successfully extracted candidate to normal output staging."""
    path, record = _owned(gp, run_dir, cid, name)
    if record["state"] != "kept":
        raise RuntimeError("candidate retention was not armed before extraction")
    gp._replace_owner(path, record, {**record, "state": "active"})


def _write_receipt(path: Path, receipt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending.json")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def retain_failed_candidate(
    gp,
    *,
    run_dir: Path,
    cid: str,
    name: str,
    baseline,
    instance: dict,
    image: str,
    generation_image_id: str | None,
    metrics: dict,
    generation_error: BaseException | None,
    workflow_log_dir: Path | None,
    task_id: str | None,
    trajectory_path: str | None,
) -> Path:
    """Keep the original baseline and publish a zero-model recovery entry."""
    if baseline is None or metrics.get("patch_extraction_succeeded") is True:
        raise ValueError("candidate retention requires an unfinished trusted extraction")
    metrics["container_preservation_required"] = True
    metrics["container_retention_reason"] = "trusted_patch_extraction_incomplete"
    errors: list[str] = []
    # TemporaryDirectory otherwise erases this baseline when generate unwinds,
    # including when saving the receipt fails because the output disk is full.
    temporary = baseline.temporary_directory
    if temporary is not None:
        temporary._finalizer.detach()
        baseline.temporary_directory = None
    receipt_path = run_dir / "candidate-recovery" / name / "recovery.json"
    metrics["candidate_recovery_path"] = str(receipt_path)
    try:
        owner_path, record = _owned(gp, run_dir, cid, name)
        if record["state"] != "kept":
            gp.mark_container_kept(run_dir, cid)
        metrics["container_retained"] = True
    except BaseException as exc:
        errors.append(f"ownership preservation failed {type(exc).__name__} {exc}")
        owner_path = gp.container_owner_path(run_dir, name)

    if temporary is not None:
        original_root = Path(temporary.name)
        destination = receipt_path.parent / "trusted-baseline"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            git_relative = baseline.git_dir.relative_to(original_root)
            links = [(key, path.relative_to(original_root)) for key, path in baseline.gitlink_state_repositories]
            if destination.exists():
                raise RuntimeError("candidate recovery baseline destination already exists")
            copied = False
            try:
                original_root.rename(destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.copytree(original_root, destination)
                copied = True
            baseline.git_dir = destination / git_relative
            baseline.gitlink_state_repositories = tuple((key, destination / relative) for key, relative in links)
            if copied:
                # The receipt uses the completed destination before the old
                # temporary copy is removed, including if removal fails.
                shutil.rmtree(original_root)
        except BaseException as exc:
            errors.append(f"baseline relocation failed {type(exc).__name__} {exc}")

    package_source = Path(gp.__file__).resolve().parents[2]
    receipt = {
        "created_epoch": time.time(),
        "reason": "trusted_patch_extraction_incomplete",
        "container_id": cid,
        "container_name": name,
        "owner_record": str(owner_path),
        "instance_id": instance.get("instance_id"),
        "original_image": image,
        "generation_image_id": generation_image_id,
        "original_base_commit": instance.get("base_commit"),
        "snapshot": asdict(baseline.snapshot),
        "baseline": {
            "git_dir": str(baseline.git_dir),
            "archive_sha256": baseline.archive_sha256,
            "archive_bytes": baseline.archive_bytes,
            "archive_entries": baseline.archive_entries,
            "extracted_bytes": baseline.extracted_bytes,
            "gitlink_worktrees": baseline.gitlink_worktrees,
            "gitlink_state_repositories": [[key, str(path)] for key, path in baseline.gitlink_state_repositories],
        },
        "trace": {
            "workflow_log_dir": str(workflow_log_dir) if workflow_log_dir else None,
            "task_id": task_id,
            "trajectory_root": str(workflow_log_dir / "trajectories" / task_id)
            if workflow_log_dir and task_id
            else None,
            "trajectory_path": str(trajectory_path) if trajectory_path else None,
        },
        "generation_error_type": type(generation_error).__name__ if generation_error else None,
        "patch_extraction_succeeded": metrics.get("patch_extraction_succeeded"),
        "existing_stop_fields": {
            key: value
            for key, value in metrics.items()
            if key
            in {"failure_origin", "error", "stop_reason", "stop_signal", "session_quiesced", "original_workflow_error"}
        },
        "recovery_environment": {"PYTHONPATH": str(package_source)},
        "recovery_argv": [
            sys.executable,
            "-m",
            "opencollab_eval.generation.candidate_retention",
            "--source-root",
            str(package_source),
            "--receipt",
            str(receipt_path),
        ],
        "preservation_errors": errors,
    }
    try:
        _write_receipt(receipt_path, receipt)
    except BaseException as exc:
        errors.append(f"recovery receipt write failed {type(exc).__name__} {exc}")
    if errors:
        detail = " | ".join(errors)
        metrics["candidate_retention_error"] = detail
        raise RuntimeError(f"candidate container {cid} retained for recovery; baseline {baseline.git_dir}; {detail}")
    return receipt_path


def recover_candidate(source_root: Path, receipt_path: Path) -> dict:
    """Reuse the fixed baseline and trusted extractor without invoking a model."""
    import opencollab_eval

    if Path(opencollab_eval.__file__).resolve().parent.parent != source_root.resolve():
        raise RuntimeError("recovery must use the selected installed runtime")
    from opencollab_eval.generation import gen_prediction as gp
    from opencollab_eval.generation.gen_prediction_patch import (
        TrustedPatchBaseline,
        extract_patch_guarded,
    )
    from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot

    receipt = json.loads(receipt_path.read_text())
    owner_path = Path(receipt["owner_record"])
    record = gp._read_owner(owner_path)
    if (
        record is None
        or record.get("state") != "kept"
        or record.get("container_id") != receipt["container_id"]
        or record.get("container_name") != receipt["container_name"]
        or gp._container_owner_label_state(receipt["container_id"], record["owner_token"]) != "matching"
    ):
        raise RuntimeError("recovery ownership verification failed")
    if gp._owner_is_live(record):
        raise RuntimeError("recovery refused while original generation owner is live")
    b = receipt["baseline"]
    snapshot = SolverGitSnapshot(**receipt["snapshot"])
    baseline = TrustedPatchBaseline(
        snapshot=snapshot,
        temporary_directory=None,
        git_dir=Path(b["git_dir"]),
        archive_sha256=b["archive_sha256"],
        archive_bytes=b["archive_bytes"],
        archive_entries=b["archive_entries"],
        extracted_bytes=b["extracted_bytes"],
        gitlink_worktrees=tuple(tuple(pair) for pair in b["gitlink_worktrees"]),
        gitlink_state_repositories=tuple((key, Path(path)) for key, path in b["gitlink_state_repositories"]),
    )
    patch, removed, proof = extract_patch_guarded(receipt["container_id"], baseline)
    target = receipt_path.parent / "recovered-candidate.patch"
    with target.open("x", encoding="utf-8") as stream:
        stream.write(patch)
    result = {
        "container_id": receipt["container_id"],
        "patch_path": str(target),
        "trusted_patch_extraction": proof,
        "removed_validation_artifacts": removed,
        "model_calls": 0,
        "official_evaluation_performed": False,
    }
    _write_receipt(receipt_path.parent / "recovered-candidate.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(recover_candidate(args.source_root, args.receipt), ensure_ascii=False))
