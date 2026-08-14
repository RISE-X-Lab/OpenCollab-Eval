from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
    pytest,
    subprocess,
    sys,
    threading,
)


def test_remote_spawn_guard_does_not_block_sigterm_in_exec_child(tmp_path):
    namespace = _remote_namespace(tmp_path)
    spawn_signal_state = namespace["block_spawn_signals"]()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    namespace["ACTIVE_CHILD_PGIDS"].add(proc.pid)
    namespace["restore_spawn_signals"](spawn_signal_state)
    try:
        namespace["os"].killpg(proc.pid, namespace["signal"].SIGTERM)
        returncode = proc.wait(timeout=1)
    finally:
        namespace["ACTIVE_CHILD_PGIDS"].discard(proc.pid)
        if proc.poll() is None:
            namespace["os"].killpg(proc.pid, namespace["signal"].SIGKILL)
            proc.wait(timeout=1)

    assert returncode == -namespace["signal"].SIGTERM


def test_remote_active_child_cleanup_escalates_to_kill_and_proves_absence(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    namespace["ACTIVE_CHILD_PGIDS"].add(424299)
    monkeypatch.setitem(namespace, "PROCESS_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setitem(namespace, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.0)
    signals = []
    probes = 0

    def group_exists(_pgid):
        nonlocal probes
        probes += 1
        return probes == 1

    monkeypatch.setitem(namespace, "process_group_exists", group_exists)
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    assert namespace["terminate_active_children"]() is True
    assert signals == [
        (424299, namespace["signal"].SIGTERM),
        (424299, namespace["signal"].SIGKILL),
    ]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()


def test_completed_generation_reuse_requires_current_immutable_image_id(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    namespace["ensure_image"] = lambda image: {
        "ok": True,
        "image_id": "sha256:" + "9" * 64,
    }

    changed = namespace["generation_for_task_once"]({"instance_id": task})

    assert changed["status"] == "technical_generation_image_identity_failed"
    assert changed["observed_generation_image_id"] == "sha256:" + "8" * 64
    assert changed["expected_generation_image_id"] == "sha256:" + "9" * 64

    namespace["ensure_image"] = lambda image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    reused = namespace["generation_for_task_once"]({"instance_id": task})
    assert reused["status"] == "generation_done"


def test_incomplete_workflow_with_proven_candidate_continues_to_official_eval(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="incomplete",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[],
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["workflow_status"] == "incomplete"
    assert result["submission_eligible"] is True


def test_blocked_workflow_with_proven_candidate_continues_to_official_eval(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="blocked",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[],
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["workflow_status"] == "blocked"
    assert result["submission_eligible"] is True


def test_incomplete_workflow_without_proven_eligibility_remains_failed(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="incomplete",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[],
        submission_eligible=False,
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "generation_failed"


@pytest.mark.parametrize(
    ("removed_field", "updates"),
    [
        ("error", {}),
        ("agent_failures", {}),
        (None, {"error": "workflow crashed"}),
        (None, {"error": ["invalid error evidence"]}),
        (None, {"agent_failures": [{"label": "reviewer", "status": "failed"}]}),
        (None, {"provider_failure": {"status": "provider_request_rejected"}}),
        (None, {"provider_failure": {}}),
        (None, {"provider_failure": None}),
        (None, {"runtime_status": "failed"}),
        (None, {"runner_returncode": 2}),
        (None, {"trusted_patch_extraction": {}}),
    ],
)
def test_incomplete_workflow_rejects_unproven_runtime_state(
    tmp_path, removed_field, updates
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="incomplete",
        runner_returncode=1,
        runtime_status="completed",
        error=None,
        agent_failures=[],
    )
    if removed_field:
        metrics[0].pop(removed_field)
    metrics[0].update(updates)
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "generation_failed"


def test_structured_provider_failure_rejects_a_conflicting_nonempty_candidate(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0]["workflow_status"] = "provider_request_rejected"
    metrics[0]["agent_failures"] = [
        {
            "label": "solver",
            "exception_type": "PermissionDeniedError",
            "status_code": 403,
            "provider_error_type": "access_terminated_error",
        }
    ]
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "technical_generation_provider_evidence_invalid"
    assert result["failure_scope"] == "task"


def test_eval_only_accepts_exact_proven_candidate_interrupted_by_provider(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0].update(
        workflow_status="incomplete",
        runner_returncode=1,
        runtime_status="failed",
        error="provider request failed after the candidate was frozen",
        agent_failures=[
            {
                "label": "solver",
                "exception_type": "PermissionDeniedError",
                "status_code": 403,
                "provider_error_type": "access_terminated_error",
            }
        ],
        provider_failure={"status": "provider_request_rejected"},
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }

    normal = namespace["generation_for_task_once"]({"instance_id": task})

    assert normal["status"] == "technical_generation_provider_evidence_invalid"

    namespace["eval_only"] = True
    recovered = namespace["generation_for_task_once"]({"instance_id": task})

    assert recovered["status"] == "generation_done"
    assert recovered["workflow_status"] == "incomplete"
    assert recovered["record_id"] == "r1"
    assert recovered["patch_sha256"] == metrics[0]["patch_sha256"]
    assert recovered["submission_integrity"] == "proven"


def test_eval_only_rejects_legacy_candidate_interrupted_by_provider(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    for field in _proven_submission_integrity():
        metrics[0].pop(field, None)
    metrics[0].update(
        agent_failures=[
            {
                "label": "solver",
                "exception_type": "PermissionDeniedError",
                "status_code": 403,
                "provider_error_type": "access_terminated_error",
            }
        ],
        provider_failure={"status": "provider_request_rejected"},
    )
    _write_jsonl(metrics_path, metrics)
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    namespace["eval_only"] = True

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "technical_generation_provider_evidence_invalid"


def test_eval_only_reconciles_derived_eval_patch_identity_before_evaluation(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        eval_only=True,
        expected_task="task-1",
        expected_record_id="r1",
        expected_source_patch_sha256="a" * 64,
        expected_eval_patch_sha256="b" * 64,
    )

    matching = namespace["reconcile_eval_only_candidate_identity"](
        {
            "status": "generation_done",
            "task": "task-1",
            "record_id": "r1",
            "source_patch_sha256": "a" * 64,
            "eval_patch_sha256": "b" * 64,
        }
    )
    drifted = namespace["reconcile_eval_only_candidate_identity"](
        {
            "status": "generation_done",
            "task": "task-1",
            "record_id": "r1",
            "source_patch_sha256": "a" * 64,
            "eval_patch_sha256": "c" * 64,
        }
    )
    task_drifted = namespace["reconcile_eval_only_candidate_identity"](
        {
            "status": "generation_done",
            "task": "task-2",
            "record_id": "r1",
            "source_patch_sha256": "a" * 64,
            "eval_patch_sha256": "b" * 64,
        }
    )

    assert matching["status"] == "generation_done"
    assert drifted["status"] == "generation_done"
    assert drifted["artifact_identity_warnings"] == [
        "stale_expected_eval_patch_sha256"
    ]
    assert drifted["candidate_identity_reconciliation"][
        "observed_candidate_identity"
    ]["eval_patch_sha256"] == "c" * 64
    assert task_drifted["status"] == "candidate_identity_mismatch"
    assert task_drifted["identity_mismatch_fields"] == ["task"]
    assert task_drifted["observed_candidate_identity"]["task"] == "task-2"


def test_eval_only_accepts_omitted_expected_eval_patch_identity(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        eval_only=True,
        expected_task="task-1",
        expected_record_id="r1",
        expected_source_patch_sha256="a" * 64,
        expected_eval_patch_sha256="",
    )

    result = namespace["reconcile_eval_only_candidate_identity"](
        {
            "status": "generation_done",
            "task": "task-1",
            "record_id": "r1",
            "source_patch_sha256": "a" * 64,
            "eval_patch_sha256": "c" * 64,
        }
    )

    assert result["status"] == "generation_done"
    assert "artifact_identity_warnings" not in result


def test_eval_only_without_manual_identity_assertion_keeps_candidate(tmp_path):
    namespace = _remote_namespace(tmp_path, eval_only=True)
    candidate = {
        "status": "generation_done",
        "task": "task-1",
        "record_id": "r1",
        "source_patch_sha256": "a" * 64,
        "eval_patch_sha256": "c" * 64,
    }

    assert namespace["reconcile_eval_only_candidate_identity"](candidate) == candidate


def _seed_remote_provider_failure(namespace, task, *, run_id=None, corrupt_snapshot=False):
    run_dir = namespace["base_run_dir"] / task
    empty_sha = namespace["patch_sha"]("")
    proof = _proven_submission_integrity("")
    if corrupt_snapshot:
        proof["solver_git_snapshot"] = {
            **proof["solver_git_snapshot"],
            "workspace_integrity": {
                **proof["solver_git_snapshot"]["workspace_integrity"],
                "outcome": "technical_failure",
                "failure_scope": "image",
            },
        }
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [{"instance_id": task, "record_id": "provider-r1", "patch_sha256": empty_sha, "model_patch": ""}],
    )
    metric = {
        "instance_id": task,
        "record_id": "provider-r1",
        "patch_sha256": empty_sha,
        "workflow_status": "provider_request_rejected",
        "agent_failures": [{"status_code": 403, "provider_error_type": "access_terminated_error"}],
        **proof,
    }
    if run_id is not None:
        metric["run_id"] = run_id
    _write_jsonl(run_dir / "metrics.jsonl", [metric])


def test_structured_provider_failure_from_an_old_run_is_regenerated(tmp_path):
    namespace = _remote_namespace(tmp_path, dry_run=True, run_id="current-run")
    task = "task-1"
    _seed_remote_provider_failure(namespace, task, run_id="old-run")
    namespace["ensure_image"] = lambda _image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["image_repo_workdir_status"] = lambda _image: {"ok": True, "status": "verified"}

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "would_generate"


def test_structured_provider_failure_rejects_a_corrupt_solver_snapshot(tmp_path):
    namespace = _remote_namespace(tmp_path, run_id="current-run")
    task = "task-1"
    _seed_remote_provider_failure(namespace, task, corrupt_snapshot=True)
    namespace["ensure_image"] = lambda _image: {"ok": True, "image_id": "sha256:" + "8" * 64}

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "technical_generation_provider_evidence_invalid"
    assert result["failure_scope"] == "task"


def test_generation_timeout_recovers_completed_candidate(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
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
                            **_proven_submission_integrity(),
                        }
                    ],
                )
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(namespace["os"], "killpg", lambda *args, **kwargs: None)
    namespace["ensure_image"] = lambda image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["returncode"] == 124
    assert result["timed_out"] is True


def test_generation_timeout_reports_stubborn_kill_reap(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path, task_wall_timeout=1)
    release = threading.Event()
    consumer_started = threading.Event()
    task = "task-1"
    signals = []

    class StubbornProcess:
        pid = 424243

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    try:
        result = namespace["generation_for_task"]({"instance_id": task})
    finally:
        release.set()

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert signals == [
        (424243, namespace["signal"].SIGTERM),
        (424243, namespace["signal"].SIGKILL),
    ]
    assert consumer_started.wait(timeout=0.2)


def test_generation_normal_exit_cleanup_failure_is_technical(monkeypatch, tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"

    class ReapedLeader:
        pid = 424262

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    namespace["ensure_image"] = lambda image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: False

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "technical_generation_cleanup_failed"
    assert result["returncode"] == namespace["PROCESS_CLEANUP_FAILED_EXIT_CODE"]
    assert 424262 in namespace["ACTIVE_CHILD_PGIDS"]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_verified_gitlink_only_child_forces_exactly_one_new_generation(tmp_path):
    namespace = _remote_namespace(
        tmp_path,
        max_empty_patch_retries=1,
        max_task_starts=2,
    )
    task = "task-1"
    calls = []

    def fake_generation_once(row, *, reuse_existing_empty_patch=True):
        calls.append(reuse_existing_empty_patch)
        if len(calls) == 1:
            return {
                "status": "empty_patch",
                "task": row["instance_id"],
                "record_id": "raw-gitlink-only",
                "submission_integrity": "filtered_empty_patch_proven",
                "source_patch_sha256": "1" * 64,
                "eval_patch_sha256": namespace["patch_sha"](""),
                "filtered_patch_paths": [
                    {
                        "path": "e",
                        "reason": "missing_snapshot_gitlink",
                        "old_oid": "2" * 40,
                        "base_oid": "2" * 40,
                        "probe_status": "verified",
                    }
                ],
            }
        return {
            "status": "generation_done",
            "task": row["instance_id"],
            "record_id": "new-source-candidate",
        }

    namespace["generation_for_task_once"] = fake_generation_once
    namespace["start_count"] = lambda run_dir: 1

    result = namespace["generation_for_task"]({"instance_id": task})

    assert calls == [True, False]
    assert result["status"] == "generation_done"
    assert result["generation_attempt_count"] == 2
    assert result["empty_patch_retry_count"] == 1
    events = namespace["read_jsonl"](namespace["base_run_dir"] / "events.jsonl")
    assert [event["phase"] for event in events] == ["empty_patch_retry"]
    assert events[0]["previous_record_id"] == "raw-gitlink-only"


def test_generation_does_not_reuse_a_candidate_from_an_old_dataset_base(tmp_path):
    namespace = _remote_namespace(tmp_path, dry_run=True)
    task = "task-1"
    namespace["image_repo_workdir_status"] = lambda _image: {
        "ok": True,
        "status": "verified",
    }
    namespace["ensure_image"] = lambda _image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    _seed_remote_completed_generation(namespace, task)

    result = namespace["generation_for_task_once"](
        {"instance_id": task, "base_commit": "f" * 40}
    )

    assert result["status"] == "would_generate"


def test_generation_classifies_verified_empty_child_as_retryable_empty_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    source_patch = "diff --git a/e b/e\ndeleted file mode 160000\n"
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": namespace["patch_sha"](source_patch),
        "model_patch": source_patch,
    }
    metric = {"workflow_status": "done"}
    namespace["prepare_eval_patch_selection"] = lambda *args: {
        "ok": True,
        "status": "ready",
        "model_patch": "",
        "source_patch_sha256": namespace["patch_sha"](source_patch),
        "eval_patch_sha256": namespace["patch_sha"](""),
        "filtered_patch_paths": [
            {
                "path": "e",
                "reason": "missing_snapshot_gitlink",
                "old_oid": "1" * 40,
                "base_oid": "1" * 40,
                "probe_status": "verified",
            }
        ],
        "gitlink_probe": {"status": "verified"},
    }

    result = namespace["_generation_patch_result"](
        {"instance_id": task},
        task,
        prediction,
        metric,
        "record_id",
    )

    assert result["status"] == "empty_patch"
    assert result["submission_integrity"] == "filtered_empty_patch_proven"
    assert result["patch_len"] == 0
    assert result["eval_patch_sha256"] == namespace["patch_sha"]("")


def test_generation_wait_system_exit_terminates_child_and_re_raises(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class InterruptedProcess:
        pid = 424250

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise SystemExit(77)
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: InterruptedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 77
    assert signals == [(424250, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_FIFO_PATHS"] == set()


def test_generation_spawn_registration_precedes_pending_signal_restore(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    signals = []

    class SpawnedProcess:
        pid = 424252

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: SpawnedProcess(),
    )
    monkeypatch.setattr(
        namespace["os"],
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    namespace["ensure_image"] = lambda image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    real_restore = namespace["restore_spawn_signals"]

    def restore_then_interrupt(previous_mask):
        real_restore(previous_mask)
        assert 424252 in namespace["ACTIVE_CHILD_PGIDS"]
        raise SystemExit(78)

    namespace["restore_spawn_signals"] = restore_then_interrupt

    with pytest.raises(SystemExit) as exc:
        namespace["generation_for_task"]({"instance_id": task})

    assert exc.value.code == 78
    assert signals == [(424252, namespace["signal"].SIGTERM)]
    assert namespace["ACTIVE_CHILD_PGIDS"] == set()
    assert namespace["ACTIVE_FIFO_PATHS"] == set()
