from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _direct_eval_report(*, task: str, record_id: str, patch_sha256: str, resolved: bool) -> dict:
    f2p_status = 0 if resolved else 1
    f2p_evidence = {
        "status": f2p_status,
        "command_matches_plan": True,
        "log_artifact_safe": True,
        "target_proof_matches_plan": f2p_status == 0,
        "target_failure_proof_matches_plan": f2p_status != 0,
        "artifact_safe": True,
    }
    target = f"tests/{task.replace('-', '_')}.py::test_case"
    f2p_plan = {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "pytest",
        "coverage": "parser_backed_exact_targets",
        "coverage_verified": True,
        "declared_targets": [target],
        "target_batches": [[target]],
        "commands": ["pytest target"],
        "proofs": [{"kind": "pytest_structured_reports", "targets": [target]}],
    }
    p2p_plan = {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "unsupported",
        "coverage": "none",
        "coverage_verified": False,
        "declared_targets": [],
        "target_batches": [],
        "commands": [],
        "proofs": [],
    }
    return {
        "schema": "opencollab.prolite_direct_eval.v2",
        "status": "done",
        "instance_id": task,
        "resolved": resolved,
        "technical_reasons": [],
        "output_artifact_errors": [],
        "docker_exit": 0,
        "cleanup_quiesced": True,
        "container_cleanup": {"ok": True},
        "patch_sha256": patch_sha256,
        "record_id": record_id,
        "eval_image_id": "sha256:" + "9" * 64,
        "eval_spec_sha256": "e" * 64,
        "tests_status": {
            "base_commit_status": 0,
            "service_bootstrap_status": 0,
            "before_repo_status": 0,
            "post_before_base_status": 0,
            "model_patch_status": 0,
            "test_patch_status": 0,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": 0,
            "fail_to_pass_plan": f2p_plan,
            "pass_to_pass_plan": p2p_plan,
            "fail_to_pass_evidence": [f2p_evidence],
            "pass_to_pass_evidence": [],
        },
    }


def _fact_report(path: Path, *, name: str, resolved: set[int]) -> Path:
    tasks = []
    official_reports = {}
    official_report_path = path.with_name(f"{path.stem}.official.json")
    for index in range(1, 101):
        verdict = index in resolved
        task = f"task-{index}"
        record_id = f"record-{name}-{index}"
        patch_sha256 = _sha(f"{name}-{index}")
        tasks.append(
            {
                "index": index,
                "task": task,
                "generation_status": "generation_done",
                "eval_status": "eval_done",
                "eval_success": True,
                "eval_pending": False,
                "resolved": verdict,
                "technical_failed": False,
                "technical_reasons": [],
                "record_id": record_id,
                "patch_sha256": patch_sha256,
                "direct_execution_proven": True,
                "report_path": str(official_report_path),
                "attempt_count": 1,
                "eval_attempt_count": 1,
            }
        )
        official_reports[task] = _direct_eval_report(
            task=task,
            record_id=record_id,
            patch_sha256=patch_sha256,
            resolved=verdict,
        )
    write_json(official_report_path, official_reports)
    return write_json(
        path,
        {
            "schema": "opencollab.swe_eval_layer_final_report.v1",
            "expected_indices": list(range(1, 101)),
            "census_errors": [],
            "counts": {
                "tasks": 100,
                "eval_success": 100,
                "eval_pending": 0,
                "eval_failed": 0,
                "empty_patch": 0,
                "over_budget_tasks": 0,
                "resolved": len(resolved),
                "unresolved": 100 - len(resolved),
                "technical_failed_final": 0,
            },
            "tasks": tasks,
        },
    )


def _audit_manifest(path: Path, *, method: str, report: Path, resolved: set[int], dataset_sha: str) -> Path:
    evidence = path.with_name(f"{path.stem}.evidence.json")
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    runtime = {
        "opencollab_commit": "a" * 40,
        "opencollab_eval_commit": "b" * 40,
        "dataset_sha256": dataset_sha,
    }
    fact_tasks = json.loads(report.read_text(encoding="utf-8"))["tasks"]
    support_paths = {}
    for kind in ("trajectory", "candidate_identity", "network_isolation"):
        support_path = path.with_name(f"{path.stem}.{kind}.json")
        write_json(
            support_path,
            {
                "kind": kind,
                "method": method,
                "source_report_sha256": report_sha,
                "covered_indices": list(range(1, 101)),
            },
        )
        support_paths[kind] = support_path
    evidence_tasks = [
        {
            "index": task["index"],
            "task": task["task"],
            "record_id": task["record_id"],
            "patch_sha256": task["patch_sha256"],
            "trajectory_clean": True,
            "candidate_identity_verified": True,
            "network_isolated": True,
            "direct_execution_proven": True,
            "artifacts": {
                "official_report": {
                    "path": task["report_path"],
                    "sha256": hashlib.sha256(Path(task["report_path"]).read_bytes()).hexdigest(),
                },
                **{
                    kind: {
                        "path": str(support_path),
                        "sha256": hashlib.sha256(support_path.read_bytes()).hexdigest(),
                    }
                    for kind, support_path in support_paths.items()
                },
            },
        }
        for task in fact_tasks
    ]
    write_json(
        evidence,
        {
            "schema": "opencollab.swe_clean_run_evidence.v1",
            "method": method,
            "source_report_sha256": report_sha,
            "runtime": runtime,
            "covered_indices": list(range(1, 101)),
            "clean_trajectory_indices": list(range(1, 101)),
            "candidate_identity_indices": list(range(1, 101)),
            "network_isolation_indices": list(range(1, 101)),
            "direct_execution_indices": list(range(1, 101)),
            "resolved_execution_indices": sorted(resolved),
            "tasks": evidence_tasks,
        },
    )
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return write_json(
        path,
        {
            "schema": "opencollab.swe_clean_run_manifest.v1",
            "method": method,
            "source_report_sha256": report_sha,
            "expected_indices": list(range(1, 101)),
            "clean_trajectory_indices": list(range(1, 101)),
            "candidate_identity_indices": list(range(1, 101)),
            "network_isolation_indices": list(range(1, 101)),
            "direct_execution_indices": list(range(1, 101)),
            "resolved_execution_indices": sorted(resolved),
            "runtime": runtime,
            "evidence_files": [{"path": evidence.name, "sha256": evidence_sha}],
        },
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    resolved_a = {7, 11, 21, 32, 34, 35}
    resolved_b = {7, 11, 21, 32, 33}
    dataset = write_json(
        tmp_path / "dataset.json",
        [
            {
                "instance_id": f"task-{index}",
                "FAIL_TO_PASS": [f"tests/task_{index}.py::test_case"],
                "PASS_TO_PASS": [],
            }
            for index in range(1, 101)
        ],
    )
    report_a = _fact_report(tmp_path / "a.json", name="A", resolved=resolved_a)
    report_b = _fact_report(tmp_path / "b.json", name="B", resolved=resolved_b)
    dataset_sha = hashlib.sha256(dataset.read_bytes()).hexdigest()
    audit_a = _audit_manifest(
        tmp_path / "a-audit.json",
        method="Method-A",
        report=report_a,
        resolved=resolved_a,
        dataset_sha=dataset_sha,
    )
    audit_b = _audit_manifest(
        tmp_path / "b-audit.json",
        method="Method-B",
        report=report_b,
        resolved=resolved_b,
        dataset_sha=dataset_sha,
    )
    return report_a, report_b, audit_a, audit_b, dataset


def mutate_audit_evidence(manifest_path: Path, mutate) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_path = manifest_path.parent / manifest["evidence_files"][0]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    mutate(manifest, evidence)
    write_json(evidence_path, evidence)
    manifest["evidence_files"][0]["sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    write_json(manifest_path, manifest)


def mutate_official_report(manifest_path: Path, mutate, *, bind_new_hash: bool = True) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_path = manifest_path.parent / manifest["evidence_files"][0]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    official_path = Path(evidence["tasks"][0]["artifacts"]["official_report"]["path"])
    official = json.loads(official_path.read_text(encoding="utf-8"))
    mutate(official)
    write_json(official_path, official)
    if bind_new_hash:
        official_sha = hashlib.sha256(official_path.read_bytes()).hexdigest()

        def bind_hash(_manifest, document):
            for row in document["tasks"]:
                row["artifacts"]["official_report"]["sha256"] = official_sha

        mutate_audit_evidence(manifest_path, bind_hash)
    return official_path


def final_report_args(tmp_path: Path) -> SimpleNamespace:
    report_a, report_b, audit_a, audit_b, dataset = _inputs(tmp_path)
    return SimpleNamespace(
        method_a_report=report_a,
        method_a_audit_manifest=audit_a,
        method_a_name="Method-A",
        method_b_report=report_b,
        method_b_audit_manifest=audit_b,
        method_b_name="Method-B",
        dataset="swe-batch-pro-lite",
        dataset_file=dataset,
        meeting_date="2026-07-15",
        author="A&B_#1",
        narrative_json=None,
        labels_json=None,
        output_dir=tmp_path / "output",
        output_prefix="comparison",
        latex_engine="xelatex",
        latex_timeout=30.0,
    )


def fake_compile(tex_path: Path, *, output_dir: Path, latex_engine: str, timeout_seconds: float):
    assert latex_engine == "xelatex"
    assert timeout_seconds == 30.0
    pdf = output_dir / f"{tex_path.stem}.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 2048)
    return pdf, "ok"


__all__ = [
    "fake_compile",
    "final_report_args",
    "mutate_audit_evidence",
    "mutate_official_report",
    "write_json",
]
