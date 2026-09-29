"""Recovery of metric projections after a prediction has been committed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencollab_eval.commands.swebench_eval_records import prediction_is_eval_eligible
from opencollab_eval.engine.swe_eval_records import latest_paired_rows
from opencollab_eval.generation import gen_prediction_batch as batch
from opencollab_eval.generation import gen_prediction_safe_output as output


def _records(
    instance_id: str,
    record_id: str,
    *,
    patch: str = "diff --git a/a b/a\n+x\n",
    status: str = "done",
) -> tuple[dict, dict]:
    return output.build_output_records(
        instance_id=instance_id,
        model_name="model",
        patch=patch,
        record_id=record_id,
        workflow_name="single-agent",
        metrics={
            "workflow_status": status,
            "run_summary": {"status": status, "tokens": 1},
            "submission_eligible": status == "done",
            "execution_quiesced": True,
            "patch_extraction_succeeded": status == "done",
            "injected_path_cleanup_proven": True,
            "harness_artifact_exclusion_proven": True,
            "checkpoint_restore_integrity_proven": True,
            "task_stage_integrity_proven": True,
            "test_patch_isolation_failed": False,
        },
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_batch_resume_repairs_lost_projection_without_running_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = tmp_path / "instances.jsonl"
    instances.write_text('{"instance_id":"task-a"}\n', encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    predictions = out / "preds-single.jsonl"
    metrics = out / "metrics.jsonl"
    prediction, metric = _records("task-a", "attempt-1")
    real_append = output._append_jsonl_durable

    def fail_metric_projection(path: Path, row: dict) -> None:
        if path == metrics:
            raise OSError("simulated metric projection write failure")
        real_append(path, row)

    with monkeypatch.context() as failure:
        failure.setattr(output, "_append_jsonl_durable", fail_metric_projection)
        with pytest.raises(OSError, match="simulated metric projection"):
            output.append_output_records(predictions, metrics, prediction, metric)

    assert prediction_is_eval_eligible(_rows(predictions)[0])
    assert not metrics.exists()

    def unexpected_run(**kwargs: object) -> tuple[int, float]:
        pytest.fail("a persisted prediction must not rerun the model")

    monkeypatch.setattr(batch, "_run_one", unexpected_run)
    arguments = ["--instances", str(instances), "--arm", "single", "--out-dir", str(out)]
    assert batch.main(arguments) == 0
    assert _rows(predictions) == [prediction]
    assert _rows(metrics) == [metric]
    assert latest_paired_rows(_rows(predictions), _rows(metrics), "task-a").status == "record_id"

    assert batch.main(arguments) == 0
    assert _rows(metrics) == [metric]


def test_recovery_retains_failed_attempts_and_existing_partial_metrics(
    tmp_path: Path,
) -> None:
    predictions = tmp_path / "preds-single.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    old_prediction, old_metric = _records(
        "task-a", "attempt-old", patch="", status="provider_failure"
    )
    new_prediction, new_metric = _records("task-a", "attempt-new")
    other_prediction, other_metric = _records("task-b", "attempt-other")
    for prediction in (old_prediction, new_prediction, other_prediction):
        output._append_jsonl_durable(predictions, prediction)
    for metric in (new_metric, other_metric):
        output._append_jsonl_durable(metrics, metric)

    batch.recover_metric_projections(predictions)
    recovered = _rows(metrics)
    assert [row["record_id"] for row in recovered] == [
        "attempt-new", "attempt-other", "attempt-old"
    ]
    assert {row["record_id"]: row for row in recovered} == {
        "attempt-old": old_metric,
        "attempt-new": new_metric,
        "attempt-other": other_metric,
    }
    assert latest_paired_rows(_rows(predictions), recovered, "task-a").metric == new_metric

    batch.recover_metric_projections(predictions)
    assert _rows(metrics) == recovered


def test_batch_repairs_projection_after_generator_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instances = tmp_path / "instances.jsonl"
    instances.write_text('{"instance_id":"task-a"}\n', encoding="utf-8")
    out = tmp_path / "out"
    predictions = out / "preds-single.jsonl"
    metrics = out / "metrics.jsonl"
    prediction, metric = _records("task-a", "attempt-1")
    real_append = output._append_jsonl_durable

    def fake_run_one(**kwargs: object) -> tuple[int, float]:
        def fail_metric_projection(path: Path, row: dict) -> None:
            if path == metrics:
                raise OSError("simulated metric projection write failure")
            real_append(path, row)

        with monkeypatch.context() as failure:
            failure.setattr(output, "_append_jsonl_durable", fail_metric_projection)
            with pytest.raises(OSError, match="simulated metric projection"):
                output.append_output_records(predictions, metrics, prediction, metric)
        return 1, 0.1

    monkeypatch.setattr(batch, "_run_one", fake_run_one)
    assert batch.main([
        "--instances", str(instances), "--arm", "single", "--out-dir", str(out)
    ]) == 0
    assert _rows(predictions) == [prediction]
    assert _rows(metrics) == [metric]


def test_recovery_refuses_conflicting_metric_for_same_attempt(tmp_path: Path) -> None:
    predictions = tmp_path / "preds-single.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    prediction, metric = _records("task-a", "attempt-1")
    output._append_jsonl_durable(predictions, prediction)
    conflicting = {**metric, "workflow_status": "provider_failure"}
    output._append_jsonl_durable(metrics, conflicting)

    with pytest.raises(OSError, match="identity collision"):
        batch.recover_metric_projections(predictions)
    assert _rows(metrics) == [conflicting]
