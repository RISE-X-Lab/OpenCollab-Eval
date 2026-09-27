"""Fresh generation preserves candidate identity and reports configuration drift."""

from swe_v1_prolite_runner_test_support import (
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
    pytest,
)


def _run_fresh_generation_with_runtime_drift(
    namespace,
    monkeypatch,
    *,
    task: str,
    returncode: int,
    submission_eligible: bool = True,
):
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)

    class Process:
        pid = 424280

        def wait(self, timeout=None):
            del timeout
            _write_jsonl(
                run_dir / "predictions.jsonl",
                [
                    {
                        "instance_id": task,
                        "record_id": "fresh-record",
                        "patch_sha256": patch_sha,
                        "model_patch": patch,
                    }
                ],
            )
            integrity = _proven_submission_integrity(patch)
            integrity["submission_eligible"] = submission_eligible
            _write_jsonl(
                run_dir / "metrics.jsonl",
                [
                    {
                        "instance_id": task,
                        "record_id": "fresh-record",
                        "patch_sha256": patch_sha,
                        "workflow_status": "done",
                        "runner_returncode": returncode,
                        "budget": namespace["budget"] + 1,
                        **integrity,
                    }
                ],
            )
            return returncode

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: Process(),
    )
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda _proc: True
    return namespace["generation_for_task_once"]({"instance_id": task})


def test_fresh_verified_generation_reports_runtime_identity_drift(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)

    result = _run_fresh_generation_with_runtime_drift(
        namespace,
        monkeypatch,
        task="task-1",
        returncode=0,
    )

    assert result["status"] == "technical_generation_identity_failed"
    assert result["reason"] == "generation_runtime_identity_mismatch"
    assert result["mismatched_identity_fields"] == ["budget"]
    assert result["record_id"] == "fresh-record"
    assert result["artifact_identity_status"] == "verified"
    assert result["patch_len"] > 0


@pytest.mark.parametrize(
    ("returncode", "submission_eligible"),
    [(1, True), (0, False)],
)
def test_fresh_generation_failure_evidence_remains_failed(
    monkeypatch,
    tmp_path,
    returncode,
    submission_eligible,
):
    namespace = _remote_namespace(tmp_path)

    result = _run_fresh_generation_with_runtime_drift(
        namespace,
        monkeypatch,
        task="task-1",
        returncode=returncode,
        submission_eligible=submission_eligible,
    )

    assert result["status"] == "generation_failed"
    assert result.get("fresh_historical_completion") is None


def test_stale_verified_record_is_not_promoted_after_a_noop_process(
    monkeypatch,
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    _seed_remote_completed_generation(namespace, task)
    metrics_path = namespace["base_run_dir"] / task / "metrics.jsonl"
    metrics = namespace["read_jsonl"](metrics_path)
    metrics[0]["budget"] = namespace["budget"] + 1
    _write_jsonl(metrics_path, metrics)

    class Process:
        pid = 424281

        def wait(self, timeout=None):
            del timeout
            return 0

    monkeypatch.setattr(
        namespace["subprocess"],
        "Popen",
        lambda *args, **kwargs: Process(),
    )
    namespace["ensure_image"] = lambda _image: {
        "ok": True,
        "image_id": "sha256:" + "8" * 64,
    }
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda _proc: True

    result = namespace["generation_for_task_once"]({"instance_id": task})

    assert result["status"] == "generation_failed"
    assert result["record_id"] == "r1"
