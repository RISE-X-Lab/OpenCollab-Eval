"""Shared preparation for swe v1 prolite runner evaluation legacy."""

# ruff: noqa: E501, F403, F405

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys

from tests.support.swe_v1_prolite_runner_test_support import *


def run_remote_runner_eval_only_uses_existing_patch_without_starting_generation(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-eval-only"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps(
            {
                "instance_id": task,
                "dockerhub_tag": "fake.image",
                "repo_language": "go",
                "fail_to_pass": ["pkg/feature_test.go::TestFeature"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    (run_dir / "predictions.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "model_patch": patch, "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "workflow_status": "done", "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    snapshot_json = shlex.quote(json.dumps(eval_snapshot_proof_fields()))
    runtime_json = shlex.quote(
        json.dumps(
            {
                "schema": "opencollab.eval_runtime_dependencies.v1",
                "phase": "restored",
                "source": "pinned_image_runtime_with_trusted_public_preparation",
                "solver_visible": False,
                "spec_sha256": hashlib.sha256(b"[]").hexdigest(),
                "entries": [],
            }
        )
    )
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then "
        "echo 'sha256:9999999999999999999999999999999999999999999999999999999999999999'; exit 0; fi\n"
        "if [ \"$1\" = \"run\" ]; then\n"
        "  output=\"\"; input=\"\"\n"
        "  for arg in \"$@\"; do case \"$arg\" in *:/eval_output) output=\"${arg%:/eval_output}\" ;; *:/eval_input:ro) input=\"${arg%:/eval_input:ro}\" ;; esac; done\n"
        "  mkdir -p \"$output\"\n"
            "  for name in base_commit before_repo post_before_base service_bootstrap model_patch test_patch f2p p2p; do "
            "echo 0 > \"$output/$name.exit\"; done\n"
            f"  echo {snapshot_json} > \"$output/base_snapshot.json\"\n"
            f"  echo {runtime_json} > \"$output/runtime_dependencies.json\"\n"
        "  python3 -c 'import json,sys; e=json.load(open(sys.argv[1]+\"/candidate_expectation.json\")); "
        "p={\"schema\":\"opencollab.eval_candidate_projection.v1\",\"status\":\"verified\","
        "**{k:v for k,v in e.items() if k != \"schema\"},\"base_commit\":\"a\"*40,"
        "\"base_tree\":\"b\"*40,\"candidate_tree\":\"c\"*40,"
        "\"generation_tree_matches\":None}; json.dump(p,open(sys.argv[2]+\"/candidate_projection.json\",\"w\"))' "
        "\"$input\" \"$output\"\n"
        "  echo 0 > \"$output/f2p.batch_001.exit\"\n"
        "  echo \"go test -count=1 -json ./pkg -run '^TestFeature$'\" > \"$output/f2p.batch_001.command\"\n"
        "  echo '{\"Action\":\"run\",\"Package\":\"example.org/project/pkg\",\"Test\":\"TestFeature\"}' > \"$output/f2p.batch_001.log\"\n"
        "  echo '{\"Action\":\"pass\",\"Package\":\"example.org/project/pkg\",\"Test\":\"TestFeature\"}' >> \"$output/f2p.batch_001.log\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    cfg = {
        "token": "dummy",
        "invocation_id": "a" * 32,
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
        "session_prefix": "test",
        "remote_proxy_base_url": "http://127.0.0.1:1",
        "start_index": 1,
        "limit": 1,
        "budget": 1,
        "max_steps": 1,
        "swe_timeout": 1,
        "task_wall_timeout": 1,
        "eval_timeout": 10,
        "llm_timeout": 1,
        "checkpoint_interval": 1,
        "max_task_starts": 1,
        "max_eval_attempts": 1,
        "eval_only": True,
        "expected_task": task,
        "expected_record_id": "existing",
        "expected_source_patch_sha256": patch_sha,
        "expected_eval_patch_sha256": "0" * 64,
        "eval_dir_name": "official_eval_fresh",
        "dry_run": False,
    }
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    previous_umask = os.umask(0o077)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", REMOTE_TEST_RUNNER],
            input=json.dumps(_complete_remote_config(cfg)),
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    finally:
        os.umask(previous_umask)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["eval_only"] is True
    assert summary["eval_dir_name"] == "official_eval_fresh"
    assert summary["preflight"]["proxy_health"]["status"] == "skipped_eval_only"
    assert summary["preflight"]["remote_repo_exists"] is False
    assert summary["preflight"]["remote_runtime_required"] is False
    assert summary["counts"]["resolved"] == 1
    assert summary["solver_attribution"] == "historical_artifact"
    assert summary["rows"][0]["generation"]["eval_only"] is True
    assert summary["rows"][0]["generation"]["artifact_identity_status"] == "legacy_unknown"
    assert summary["rows"][0]["generation"]["artifact_identity_warnings"] == [
        "stale_expected_eval_patch_sha256"
    ]
    assert summary["rows"][0]["generation"]["candidate_identity_reconciliation"][
        "status"
    ] == "accepted_recomputed_eval_patch"
    eval_dir = run_dir / "official_eval_fresh"
    assert eval_dir.stat().st_mode & 0o777 == 0o755
    assert (eval_dir / "input").stat().st_mode & 0o777 == 0o755
    assert (eval_dir / "reports").stat().st_mode & 0o777 == 0o755
    assert (eval_dir / "input" / "base_commit").stat().st_mode & 0o777 == 0o644
    assert (eval_dir / "input" / "run_prolite_direct_eval.sh").stat().st_mode & 0o777 == 0o755
    assert (run_dir / "official_eval_fresh" / "summary.json").exists()

