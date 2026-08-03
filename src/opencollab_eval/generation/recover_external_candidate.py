"""Recover a frozen external-solver candidate without another model call."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from opencollab_eval.engine.swe_generation_proof import MAX_TRUSTED_PATCH_BYTES

from . import container_quiescence
from .external_solver_usage import _external_solver_evidence
from .gen_prediction_constants import DOCKER_WORKDIR

RECOVERY_SCHEMA = "opencollab.external_solver_recovery.v1"
SOLVER_TASK_RE = re.compile(r"solver-[0-9a-f]{32}")
COPY_LIMITS = {
    "claude.patch": MAX_TRUSTED_PATCH_BYTES,
    "claude.prompt.md": 16 * 1024 * 1024,
    "claude.settings.json": 256 * 1024,
    "claude.stream.jsonl": 512 * 1024 * 1024,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, limit: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"recovery source artifact is missing: {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(f"recovery source artifact is unsafe: {path.name}")


def _read_json(path: Path, *, limit: int = 1024 * 1024) -> dict[str, Any]:
    _regular_file(path, limit=limit)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"recovery source JSON is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"recovery source JSON must be an object: {path.name}")
    return value


def _normalized_solver_instance(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    normalized.pop("instance_id", None)
    return normalized


def _run_docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:2000]
        raise RuntimeError(f"candidate recovery Docker command failed: {detail}")
    return result


def _verify_container_baseline(
    container_id: str,
    *,
    anonymous_head: str,
    base_tree: str,
) -> None:
    head = _run_docker(
        "exec", "-w", DOCKER_WORKDIR, container_id, "git", "rev-parse", "HEAD"
    ).stdout.strip()
    tree = _run_docker(
        "exec",
        "-w",
        DOCKER_WORKDIR,
        container_id,
        "git",
        "rev-parse",
        "HEAD^{tree}",
    ).stdout.strip()
    if head != anonymous_head or tree != base_tree:
        raise ValueError("recovery container baseline does not match source candidate")


def _apply_patch(container_id: str, patch_path: Path) -> None:
    remote_patch = f"/tmp/opencollab-recovery-{uuid.uuid4().hex}.patch"
    _run_docker("cp", str(patch_path), f"{container_id}:{remote_patch}")
    try:
        _run_docker(
            "exec",
            "-w",
            DOCKER_WORKDIR,
            container_id,
            "git",
            "apply",
            "--binary",
            "--index",
            "--whitespace=nowarn",
            remote_patch,
        )
    finally:
        _run_docker("exec", container_id, "rm", "-f", remote_patch)


def recover_candidate(
    *,
    source_dir: Path,
    output_dir: Path,
    container_id: str,
    solver_task_id: str,
    instance_file: Path,
    prompt_file: Path,
    expected_source_solver_task_id: str,
    expected_raw_patch_sha256: str,
    expected_candidate_tree: str,
    expected_source_sidecar_sha256: str,
) -> dict[str, Any]:
    """Verify archived evidence, rebind its controller id, and apply its patch."""
    if SOLVER_TASK_RE.fullmatch(solver_task_id) is None:
        raise ValueError("recovery solver task id is invalid")
    try:
        source_info = source_dir.lstat()
    except OSError as exc:
        raise ValueError("recovery evidence directory is missing") from exc
    if not stat.S_ISDIR(source_info.st_mode):
        raise ValueError("recovery evidence directory is unsafe")

    source_sidecar_path = source_dir / "external_solver.sidecar.json"
    _regular_file(source_sidecar_path, limit=4 * 1024 * 1024)
    source_sidecar_sha256 = _sha256(source_sidecar_path)
    if source_sidecar_sha256 != expected_source_sidecar_sha256:
        raise ValueError("recovery source sidecar SHA does not match expectation")
    source_evidence = _external_solver_evidence(source_dir)
    if source_evidence is None or source_evidence.get("evidence_valid") is not True:
        raise ValueError("recovery source evidence is not valid")
    source_binding = source_evidence.get("invocation_binding")
    if not isinstance(source_binding, dict):
        raise ValueError("recovery source binding is missing")
    source_solver_task_id = str(source_binding.get("solver_task_id") or "")
    if SOLVER_TASK_RE.fullmatch(source_solver_task_id) is None:
        raise ValueError("recovery source solver task id is invalid")
    expected_values = {
        "solver_task_id": expected_source_solver_task_id,
        "raw_patch_sha256": expected_raw_patch_sha256,
        "candidate_tree": expected_candidate_tree,
    }
    if any(source_binding.get(key) != value for key, value in expected_values.items()):
        raise ValueError("recovery source candidate does not match expectation")

    source_instance_path = source_dir / "solver_instance.json"
    source_prompt_path = source_dir / "prompt.md"
    source_instance = _read_json(source_instance_path, limit=16 * 1024 * 1024)
    current_instance = _read_json(instance_file, limit=16 * 1024 * 1024)
    if source_instance.get("instance_id") != source_solver_task_id:
        raise ValueError("recovery source task identity is inconsistent")
    if current_instance.get("instance_id") != solver_task_id:
        raise ValueError("recovery target task identity is inconsistent")
    if _normalized_solver_instance(source_instance) != _normalized_solver_instance(
        current_instance
    ):
        raise ValueError("recovery source task specification does not match current task")
    _regular_file(source_prompt_path, limit=16 * 1024 * 1024)
    _regular_file(prompt_file, limit=16 * 1024 * 1024)
    if source_prompt_path.read_bytes() != prompt_file.read_bytes():
        raise ValueError("recovery source prompt does not match current task")

    current_image_id = container_quiescence.container_image_id(container_id)
    if current_image_id != source_binding.get("task_image_id"):
        raise ValueError("recovery task image does not match source candidate")
    _verify_container_baseline(
        container_id,
        anonymous_head=str(source_binding["anonymous_head"]),
        base_tree=str(source_binding["base_tree"]),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name, limit in COPY_LIMITS.items():
        source = source_dir / name
        _regular_file(source, limit=limit)
        target = output_dir / name
        shutil.copyfile(source, target)
        hashes[name] = _sha256(target)

    source_required_path = source_dir / "external_solver.required.json"
    required = _read_json(source_required_path)
    sidecar = _read_json(source_sidecar_path, limit=4 * 1024 * 1024)
    if required.get("solver_task_id") != source_solver_task_id:
        raise ValueError("recovery source controller task identity is inconsistent")
    source_required_sha256 = _sha256(source_required_path)
    required["solver_task_id"] = solver_task_id
    required.pop("network_name", None)
    sidecar["source_invocation_binding"] = dict(source_binding)
    sidecar["invocation_binding"] = {
        **source_binding,
        "solver_task_id": solver_task_id,
    }
    sidecar["candidate_ready"] = False
    recovery = {
        "schema": RECOVERY_SCHEMA,
        "source_solver_task_id": source_solver_task_id,
        "recovery_solver_task_id": solver_task_id,
        "source_required_sha256": source_required_sha256,
        "source_sidecar_sha256": source_sidecar_sha256,
        "source_instance_sha256": _sha256(source_instance_path),
        "source_prompt_sha256": _sha256(source_prompt_path),
        "artifacts": hashes,
    }
    sidecar["recovery"] = recovery
    (output_dir / "external_solver.required.json").write_text(
        json.dumps(required, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "external_solver.sidecar.json").write_text(
        json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "external_solver.recovery.json").write_text(
        json.dumps(recovery, sort_keys=True) + "\n", encoding="utf-8"
    )
    rebound = _external_solver_evidence(output_dir)
    if rebound is None or rebound.get("candidate_binding_complete") is not True:
        raise ValueError("recovered external solver evidence is incomplete")
    patch_path = output_dir / "claude.patch"
    if hashes["claude.patch"] != source_binding.get("raw_patch_sha256"):
        raise ValueError("recovery source patch SHA does not match candidate binding")
    _apply_patch(container_id, patch_path)
    return recovery


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover one frozen external-solver candidate"
    )
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--solver-task-id", required=True)
    parser.add_argument("--instance-file", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--expected-source-solver-task-id", required=True)
    parser.add_argument("--expected-raw-patch-sha256", required=True)
    parser.add_argument("--expected-candidate-tree", required=True)
    parser.add_argument("--expected-source-sidecar-sha256", required=True)
    args = parser.parse_args()
    recover_candidate(
        source_dir=Path(args.source_dir),
        output_dir=Path(args.output_dir),
        container_id=args.container_id,
        solver_task_id=args.solver_task_id,
        instance_file=Path(args.instance_file),
        prompt_file=Path(args.prompt_file),
        expected_source_solver_task_id=args.expected_source_solver_task_id,
        expected_raw_patch_sha256=args.expected_raw_patch_sha256,
        expected_candidate_tree=args.expected_candidate_tree,
        expected_source_sidecar_sha256=args.expected_source_sidecar_sha256,
    )


if __name__ == "__main__":
    main()
