"""Remote generation state and identity evidence tests."""

from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
    json,
    pytest,
)

from opencollab_eval.generation import gen_prediction as gp


def test_remote_runner_rejects_invalid_slice_config(tmp_path):
    namespace = _remote_namespace(tmp_path, start_index=0, limit=0, max_task_starts=-1)

    errors = namespace["validate_runner_config"]()

    assert "start_index must be >= 1" in errors
    assert "limit must be > 0" in errors
    assert "max_task_starts must be >= 0" in errors


def test_remote_runner_rejects_excessive_slice_limit(tmp_path):
    namespace = _remote_namespace(tmp_path, limit=1001)

    assert "limit must be <= 1000" in namespace["validate_runner_config"]()


def test_remote_generation_identity_includes_effective_thinking_default(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["generation_runtime_identity"]()["workflow_env"] == {
        "OPENCOLLAB_THINKING": "false"
    }
    assert "llm_transport" not in namespace["generation_runtime_identity"]()
    inherited = {"OPENCOLLAB_LLM_TRANSPORT": "direct"}
    namespace["bind_llm_transport_environment"](inherited)
    assert "OPENCOLLAB_LLM_TRANSPORT" not in inherited


def test_remote_generation_identity_includes_model_user_agent(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        workflow_env={"OPENCOLLAB_LLM_USER_AGENT": "compatible-client/1.0"},
    )

    assert namespace["generation_runtime_identity"]()["workflow_env"] == {
        "OPENCOLLAB_THINKING": "false",
        "OPENCOLLAB_LLM_USER_AGENT": "compatible-client/1.0",
    }
    metric = {
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    assert namespace["generation_identity_matches"]({}, metric, require_patch=False)
    metric["workflow_env"]["OPENCOLLAB_LLM_USER_AGENT"] = "different-client/1.0"
    assert not namespace["generation_identity_matches"]({}, metric, require_patch=False)


def test_remote_generation_identity_includes_tool_result_limit(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        workflow_env={"OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS": "1200"},
    )

    assert namespace["generation_runtime_identity"]()["workflow_env"] == {
        "OPENCOLLAB_THINKING": "false",
        "OPENCOLLAB_WORKFLOW_TOOL_RESULT_CHARS": "1200",
    }


def test_kimi_direct_transport_is_bound_into_generation_identity(tmp_path, monkeypatch):
    env_file = tmp_path / "kimi.env"
    env_file.write_text(
        "KIMI_API_KEY=secret\nHTTPS_PROXY=http://proxy.example.invalid:8888\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    namespace = _remote_namespace(
        tmp_path,
        token="",
        remote_api_env_file=str(env_file),
        llm_transport="direct",
        llm_model="kimi-for-coding",
        llm_provider="openai",
        remote_proxy_base_url="https://api.kimi.com/coding/v1",
    )
    child_env = {"https_proxy": "http://stale.example:8080"}
    namespace["bind_llm_transport_environment"](child_env)
    assert child_env["OPENCOLLAB_LLM_TRANSPORT"] == "direct"
    assert child_env["HTTPS_PROXY"] == "http://proxy.example.invalid:8888"
    assert "https_proxy" not in child_env
    assert "KIMI_API_KEY" not in child_env

    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    runtime_identity = namespace["generation_runtime_identity"]()
    metric = {
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **_proven_submission_integrity(patch),
        **{key: value for key, value in runtime_identity.items() if key != "llm_transport"},
    }
    monkeypatch.setenv("OPENCOLLAB_LLM_TRANSPORT", "direct")
    monkeypatch.setenv(
        "OPENCOLLAB_EVAL_LLM_BASE_URL_SHA256",
        runtime_identity["llm_base_url_sha256"],
    )
    monkeypatch.setenv(
        "OPENCOLLAB_EVAL_WORKFLOW_ENV",
        json.dumps(runtime_identity["workflow_env"]),
    )
    gp.bind_llm_transport(metric)
    prediction = {
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
        "model_patch": patch,
    }

    assert metric["llm_transport"] == "direct"
    assert namespace["generation_identity_matches"](prediction, metric) is True


def test_remote_runner_allows_eval_only_mode_with_existing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0, eval_only=True)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                "generation_image_id": "sha256:" + "8" * 64,
            }
        ],
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"


def test_remote_runner_resumes_generated_candidate_under_a_new_invocation(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        invocation_id="a" * 32,
        run_id="stable-run",
        runtime_tree_sha256="1" * 64,
        max_task_starts=1,
    )
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    run_dir = namespace["base_run_dir"] / task
    source_identity = namespace["generation_runtime_identity"]()
    namespace["write_json"](
        namespace["generation_state_path"](run_dir),
        {
            "start_count": 1,
            "starts": [
                {
                    "workflow": namespace["workflow"],
                    "model_name": namespace["model_name"],
                    "runtime_identity": source_identity,
                }
            ],
        },
    )
    namespace["invocation_id"] = "b" * 32
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    namespace["remote_root"].mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].write_text("[]\n", encoding="utf-8")
    namespace["http_health"] = lambda *_args, **_kwargs: {"ok": True}
    namespace["load_dataset"] = lambda *_args: [{"instance_id": task}]
    eval_calls = []
    namespace["eval_for_task"] = lambda row: (
        eval_calls.append(row)
        or {
            "status": "eval_done",
            "task": task,
            "attempt_count": 1,
            "summary": {"resolved": True},
        }
    )

    returncode = namespace["main"]()
    summary = namespace["load_json"](namespace["base_run_dir"] / "summary.json")

    assert returncode == 0
    assert len(eval_calls) == 1
    assert summary["rows"][0]["generation"]["status"] == "generation_done"
    assert namespace["start_count"](run_dir) == 1
    _prediction, metric, _pairing = namespace["latest_pair"](run_dir, task)
    assert metric["invocation_id"] == "a" * 32


def test_remote_runner_eval_only_mode_does_not_start_missing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0)

    result = namespace["generation_for_task"]({"instance_id": "task-1"})

    assert result["status"] == "generation_start_limit_reached"


def test_remote_runner_skips_eval_after_generation_cleanup_failure(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["remote_root"].mkdir(parents=True, exist_ok=True)
    namespace["remote_repo"].mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].write_text("[]\n", encoding="utf-8")
    namespace["http_health"] = lambda *args, **kwargs: {"ok": True}
    namespace["load_dataset"] = lambda *_args: [{"instance_id": "task-1"}]
    namespace["generation_for_task"] = lambda row: {
        "status": "technical_generation_cleanup_failed",
        "task": row["instance_id"],
    }
    eval_calls = []
    namespace["eval_for_task"] = lambda row: eval_calls.append(row) or {"status": "eval_done"}

    returncode = namespace["main"]()
    summary = json.loads((namespace["base_run_dir"] / "summary.json").read_text(encoding="utf-8"))

    assert returncode == 1
    assert eval_calls == []
    assert summary["rows"][0]["eval"] == {
        "status": "skipped_generation_not_ready",
        "task": "task-1",
        "generation_status": "technical_generation_cleanup_failed",
        "reason": "generation_not_ready",
    }


def test_remote_runner_recovers_committed_prediction_without_metrics_projection(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=1)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        **_proven_submission_integrity(patch),
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    namespace["write_json"](
        namespace["generation_state_path"](run_dir),
        {"start_count": 1},
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["pairing"] == "embedded_metric"


@pytest.mark.parametrize(
    ("status", "returncode", "expected"),
    [
        ("done", 0, True),
        ("done", 1, False),
        ("done_with_timeout_patch", 124, True),
        ("done_with_timeout_patch", 1, False),
        ("done_with_timeout_patch", 0, False),
        ("done", True, False),
        ("done", None, False),
    ],
)
def test_remote_generation_done_requires_strict_status_returncode_identity(
    tmp_path,
    status,
    returncode,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": status,
        **_proven_submission_integrity(patch),
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    if returncode is not None:
        metric["runner_returncode"] = returncode
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert done is expected
    assert pairing == "embedded_metric"


@pytest.mark.parametrize(
    ("integrity_fields", "expected"),
    [
        ({}, False),
        (
            _proven_submission_integrity(
                "diff --git a/src/a.py b/src/a.py\n+fixed\n"
            ),
            True,
        ),
        ({"submission_eligible": False}, False),
        ({"execution_quiesced": False}, False),
        ({"test_patch_isolation_failed": True}, False),
    ],
)
def test_remote_generation_done_requires_modern_submission_integrity(
    tmp_path,
    integrity_fields,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        **integrity_fields,
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](
        run_dir,
        task,
    )

    assert done is expected
    assert pairing == "embedded_metric"


def test_eval_only_can_observe_a_legacy_artifact_without_promoting_it_to_current_run(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    current, *_ = namespace["generation_done"](run_dir, task)
    historical, _prediction, _metric, _pairing = namespace["generation_done"](
        run_dir,
        task,
        require_identity=False,
    )

    assert current is False
    assert historical is True
    assert namespace["historical_generation_identity_status"](
        prediction, metric, task
    ) == "legacy_verified"
