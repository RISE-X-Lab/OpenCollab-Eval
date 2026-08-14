from __future__ import annotations

# ruff: noqa: E501, F401, F403, F405, I001

import hashlib
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_v1_prolite_runner_test_support import *
from opencollab_eval.engine.swe_v1_remote_eval_script import direct_eval_script


def test_direct_eval_script_preserves_config_and_projection_order():
    script = direct_eval_script()

    assert "chmod 0644 /eval_output/f2p.command" in script
    assert "chmod 0644 /eval_output/p2p.command" in script
    assert "GIT_CONFIG_KEY_0=safe.directory" in script
    assert 'GIT_CONFIG_VALUE_0="$repo_root"' in script
    assert "GIT_CONFIG_GLOBAL=/tmp/opencollab-eval-global.gitconfig" in script
    assert script.index("eval_candidate_projection.py") < script.index(
        "gen_prediction_snapshot_container.py --prepare-public-input"
    )


def test_remote_runner_eval_only_uses_existing_patch_without_starting_generation(tmp_path):
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


@pytest.mark.parametrize("candidate_payload", [None, "{"])
def test_remote_runner_persists_eval_attempt_cap_across_resume(tmp_path, candidate_payload):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-eval-resume"
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
    docker_runs = tmp_path / "docker_runs.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    candidate_line = (
        "  printf '{' > \"$output/candidate_projection.json\"\n" if candidate_payload else ""
    )
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then "
        "echo 'sha256:9999999999999999999999999999999999999999999999999999999999999999'; exit 0; fi\n"
        "if [ \"$1\" = \"run\" ]; then\n"
        f"  count_file={shlex.quote(str(docker_runs))}\n"
        "  count=0; [ ! -f \"$count_file\" ] || count=$(cat \"$count_file\")\n"
        "  echo $((count + 1)) > \"$count_file\"\n"
        "  output=\"\"\n"
        "  for arg in \"$@\"; do case \"$arg\" in *:/eval_output) output=\"${arg%:/eval_output}\" ;; esac; done\n"
        "  mkdir -p \"$output\"\n"
        "  for name in base_commit before_repo post_before_base service_bootstrap model_patch test_patch p2p; do echo 0 > \"$output/$name.exit\"; done\n"
        "  echo 1 > \"$output/f2p.exit\"\n"
        "  echo 'ECONNREFUSED 127.0.0.1:6379' > \"$output/f2p.log\"\n"
        + candidate_line
        + "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    cfg = {
        "token": "dummy",
        "invocation_id": "d" * 32,
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
        "eval_timeout": 1,
        "llm_timeout": 1,
        "checkpoint_interval": 1,
        "max_task_starts": 1,
        "max_eval_attempts": 2,
        "eval_only": True,
        "eval_dir_name": "official_eval_resume",
        "dry_run": False,
    }
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    first = subprocess.run(
        [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert first.returncode == 1, first.stderr
    assert docker_runs.read_text(encoding="utf-8").strip() == "2"
    assert json.loads(first.stdout)["rows"][0]["eval"]["attempt_count"] == 2
    (run_dir / "official_eval_resume" / "summary.json").unlink()

    cfg["invocation_id"] = "e" * 32
    resumed = subprocess.run(
        [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert resumed.returncode == 1, resumed.stderr
    resumed_eval = json.loads(resumed.stdout)["rows"][0]["eval"]
    assert resumed_eval["attempt_count"] == 2
    assert resumed_eval["retry_budget_exhausted"] is True
    assert resumed_eval["summary"] is None
    assert docker_runs.read_text(encoding="utf-8").strip() == "2"


def test_remote_runner_defers_blocked_image_retry_and_caps_configured_attempts(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-1"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps(
            {
                "instance_id": task,
                "dockerhub_tag": "missing.image",
                "repo_language": "go",
                "fail_to_pass": ["pkg/feature_test.go::TestFeature"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    remote_repo.mkdir()
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({
            "instance_id": task,
            "model_patch": patch,
            "record_id": "r1",
            "patch_sha256": patch_sha,
            "model_name_or_path": "model",
            "workflow": "validation-council-solve",
            "budget": 1,
            "max_steps": 1,
        }) + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps({
            "instance_id": task,
            "workflow_status": "done",
            "record_id": "r1",
            "patch_sha256": patch_sha,
            "model_name_or_path": "model",
            "workflow": "validation-council-solve",
            "budget": 1,
            "max_steps": 1,
        }) + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker_calls.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$@\" >> " + str(docker_log) + "\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 1; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    server = _NoopHealthServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = {
            "token": "dummy",
            "invocation_id": "a" * 32,
            "remote_root": str(remote_root),
            "remote_repo": str(remote_repo),
            "base_run_dir": str(base_run_dir),
            "workflow": "validation-council-solve",
            "model_name": "model",
            "session_prefix": "test",
            "remote_proxy_base_url": f"http://127.0.0.1:{server.server_port}",
            "start_index": 1,
            "limit": 1,
            "budget": 1,
            "max_steps": 1,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 1,
            "max_task_starts": 1,
            "max_eval_attempts": 5,
            "eval_only": True,
            "dry_run": False,
        }
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    finally:
        server.shutdown()

    assert proc.returncode == 1
    summary = json.loads(proc.stdout)
    assert summary["max_eval_attempts"] == 2
    assert summary["counts"]["eval_attempts"] == 0
    assert summary["counts"]["eval_retry_tasks"] == 0
    evaluation = summary["rows"][0]["eval"]
    assert evaluation["status"] == "blocked_missing_eval_image"
    assert evaluation["attempt_count"] == 0
    assert evaluation["max_eval_attempts"] == 2
    assert "attempts" not in evaluation
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert docker_calls.count("image inspect registry.example/swebench:missing.image") == 1
    assert docker_calls.count("pull registry.example/swebench:missing.image") == 1




def test_remote_runner_rebuilds_the_workspace_from_the_dataset_base_commit():
    assert 'gen_prediction_snapshot_container.py --public-preparation "$repo_root"' in REMOTE_IMPLEMENTATION_SOURCE
    assert 'gen_prediction_snapshot_container.py --prepare-public-input "$repo_root"' in REMOTE_IMPLEMENTATION_SOURCE
    assert 'git rev-list --all --count' in REMOTE_IMPLEMENTATION_SOURCE
    assert '"base_commit_status": base_commit_status' in REMOTE_IMPLEMENTATION_SOURCE
    assert 'actual_after_before="$(git rev-parse HEAD' in REMOTE_IMPLEMENTATION_SOURCE
    assert '"post_before_base_status": post_before_base_status' in REMOTE_IMPLEMENTATION_SOURCE
    assert "git status --porcelain --untracked-files=no --ignore-submodules=all" in REMOTE_IMPLEMENTATION_SOURCE
    before = REMOTE_IMPLEMENTATION_SOURCE.index("python3 /eval_input/public_preparation_runner.py")
    initial_snapshot = REMOTE_IMPLEMENTATION_SOURCE.index(
        'gen_prediction_snapshot_container.py --prepare-public-input "$repo_root"'
    )
    prepared_snapshot = REMOTE_IMPLEMENTATION_SOURCE.index(
        'gen_prediction_snapshot_container.py --public-preparation "$repo_root"'
    )
    assert initial_snapshot < before < prepared_snapshot
    assert REMOTE_IMPLEMENTATION_SOURCE.index("eval_candidate_projection.py") < initial_snapshot
    assert '"public_preparation_runner.py"' in REMOTE_IMPLEMENTATION_SOURCE
    assert 'if [ "$actual_after_before" = "$baseline_head" ]' in REMOTE_IMPLEMENTATION_SOURCE


def test_local_eval_only_skips_generation_dependencies():
    main_source = inspect.getsource(runner.main)
    payload_source = inspect.getsource(runner._controller._remote_payload)
    assert "if args.eval_only:" in main_source
    assert (
        '"token": "" if eval_only or remote_api_env_file else get_proxy_token(args.proxy_env_file)'
        in payload_source
    )
