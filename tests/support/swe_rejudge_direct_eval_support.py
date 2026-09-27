"""Shared preparation for swe rejudge direct eval."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opencollab_eval.engine.swe_v1_remote_artifacts import (
    read_eval_output_artifacts as _read_eval_output_artifacts,
)
from tests.support.generation_proof_test_support import (
    candidate_eval_proof_fields,
    candidate_source_projection_fields,
)
from tests.support.swe_v1_prolite_runner_test_support import eval_snapshot_proof_fields


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")



def _plans() -> tuple[dict, dict]:
    f2p = {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "go-test-json",
        "coverage": "exact_test_events",
        "coverage_verified": True,
        "declared_targets": ["pkg/widget_test.go::TestWidget"],
        "target_batches": [["pkg/widget_test.go::TestWidget"]],
        "commands": ["go test -count=1 -json ./pkg -run '^TestWidget$'"],
        "proofs": [
            {
                "kind": "go_json_test_pass",
                "test": "TestWidget",
                "package": "./pkg",
                "test_file": "pkg/widget_test.go",
            }
        ],
        "runtime_dependencies": [],
    }
    p2p = {
        "schema": "opencollab.prolite_test_plan.v2",
        "adapter": "unsupported",
        "coverage": "none",
        "coverage_verified": False,
        "declared_targets": [],
        "target_batches": [],
        "commands": [],
        "proofs": [],
        "runtime_dependencies": [],
    }
    return f2p, p2p



_DEFAULT_CANDIDATE_EXPECTATION = candidate_eval_proof_fields("task-1", "record-1", "a" * 64, "c" * 64)[0]



def read_eval_output_artifacts(*args, **kwargs):
    kwargs.setdefault("candidate_expectation", _DEFAULT_CANDIDATE_EXPECTATION)
    return _read_eval_output_artifacts(*args, **kwargs)



def _seed_output(
    report_dir: Path,
    f2p_plan: dict,
    *,
    candidate_identity: tuple[str, str, str, str] = ("task-1", "record-1", "a" * 64, "c" * 64),
    f2p_status: int = 0,
    f2p_log: str = (
        '{"Action":"run","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
        '{"Action":"pass","Package":"example.org/project/pkg","Test":"TestWidget"}\n'
    ),
) -> None:
    report_dir.mkdir(parents=True)
    for name in (
        "base_commit",
        "service_bootstrap",
        "before_repo",
        "post_before_base",
        "model_patch",
        "test_patch",
        "f2p",
        "p2p",
    ):
        status = f2p_status if name == "f2p" else 0
        (report_dir / f"{name}.exit").write_text(f"{status}\n", encoding="ascii")
    snapshot = eval_snapshot_proof_fields()
    _write_json(report_dir / "base_snapshot.json", snapshot)
    expectation, projection = candidate_eval_proof_fields(
        *candidate_identity,
        base_commit=snapshot["anonymous_head"],
        base_tree=snapshot["base_tree"],
        source_base_commit=snapshot["expected_base_commit"],
    )
    _write_json(report_dir / "candidate_projection.json", projection)
    _write_json(
        report_dir / "source_candidate_projection.json",
        candidate_source_projection_fields(expectation),
    )
    _write_json(
        report_dir / "runtime_dependencies.json",
        {
            "schema": "opencollab.eval_runtime_dependencies.v1",
            "phase": "restored",
            "source": "pinned_image_runtime_with_trusted_public_preparation",
            "solver_visible": False,
            "spec_sha256": hashlib.sha256(b"[]").hexdigest(),
            "entries": [],
        },
    )
    (report_dir / "f2p.batch_001.exit").write_text(f"{f2p_status}\n", encoding="ascii")
    (report_dir / "f2p.batch_001.command").write_text(
        f2p_plan["commands"][0] + "\n",
        encoding="utf-8",
    )
    (report_dir / "f2p.batch_001.log").write_text(f2p_log, encoding="utf-8")
    for name in ("f2p.command", "p2p.command"):
        path = report_dir / name
        path.write_text("diagnostic aggregate\n", encoding="utf-8")
        path.chmod(0)

