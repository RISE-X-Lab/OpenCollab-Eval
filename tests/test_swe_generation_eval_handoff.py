"""Run generation completion, patch selection, and evaluation in one namespace."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest
from swe_v1_prolite_runner_test_support import (
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _write_jsonl,
)

from opencollab_eval.generation.gen_prediction_config import validate_generation_limits
from opencollab_eval.runtime_config import resolve_runtime_config


def _fresh_generation(namespace, monkeypatch, *, metric_updates=None):
    task = "task-1"
    row = {"instance_id": task}
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    observed = []
    select = namespace["prepare_eval_patch_selection"]

    def observe_selection(row, prediction, metric, *args, **kwargs):
        before = copy.deepcopy((prediction, metric))
        files = [(run_dir / name).read_bytes() for name in ("predictions.jsonl", "metrics.jsonl")]
        ready_before = namespace["generation_done"](run_dir, task)[0]
        selected = select(row, prediction, metric, *args, **kwargs)
        assert (prediction, metric) == before
        assert files == [(run_dir / name).read_bytes() for name in ("predictions.jsonl", "metrics.jsonl")]
        assert namespace["generation_done"](run_dir, task)[0] == ready_before
        observed.append(selected["ok"])
        return selected

    namespace["prepare_eval_patch_selection"] = observe_selection

    class Process:
        pid = 424280

        def wait(self, timeout=None):
            del timeout
            _write_jsonl(run_dir / "predictions.jsonl", [{
                "instance_id": task, "record_id": "fresh-record",
                "patch_sha256": namespace["patch_sha"](patch), "model_patch": patch,
            }])
            metric = {
                "instance_id": task, "record_id": "fresh-record",
                "patch_sha256": namespace["patch_sha"](patch),
                "workflow_status": "done", "runner_returncode": 0,
                "budget": None, "max_steps": None, "context_window": 1_048_576,
                **_proven_submission_integrity(patch),
            }
            metric.update(metric_updates or {})
            _write_jsonl(run_dir / "metrics.jsonl", [metric])
            return 0

    monkeypatch.setattr(namespace["subprocess"], "Popen", lambda *args, **kwargs: Process())
    namespace["ensure_image"] = lambda _image: {"ok": True, "image_id": "sha256:" + "8" * 64}
    namespace["write_fifo_with_timeout"] = lambda *args, **kwargs: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda _proc: True
    result = namespace["generation_for_task_once"](row)
    return row, result, observed


def _configured_namespace(tmp_path, workflow, runner_transport):
    return _remote_namespace(
        tmp_path, workflow=workflow, runner_transport=runner_transport,
        model_name="gpt-5.6-luna", llm_model="gpt-5.6-luna", llm_provider="openai",
        workflow_env={"OPENCOLLAB_UNBOUNDED_LIMITS": "true"},
        context_window=1_048_576, budget=2_400_000, max_steps=84,
        run_id="run-a", runtime_tree_sha256="a" * 64,
    )


@pytest.mark.parametrize("workflow", ["single-agent", "validation-council-solve"])
@pytest.mark.parametrize("runner_transport", ["local", "ssh"])
@pytest.mark.parametrize("new_invocation", [False, True])
def test_effective_config_reaches_eval_in_same_process(
    monkeypatch, tmp_path, workflow, runner_transport, new_invocation,
):
    namespace = _configured_namespace(tmp_path, workflow, runner_transport)
    row, generation, observed = _fresh_generation(namespace, monkeypatch)
    assert generation["status"] == "generation_done"
    assert observed == [True]
    if new_invocation:
        namespace["invocation_id"] = "c" * 32
    prediction, metric, pairing = namespace["latest_pair"](
        namespace["base_run_dir"] / row["instance_id"], row["instance_id"]
    )
    assert pairing == "record_id"
    assert namespace["historical_generation_identity_status"](prediction, metric, row["instance_id"]) == "verified"
    assert namespace["current_generation_proof_valid"](metric, prediction["model_patch"])
    evaluation = namespace["eval_for_task_with_retries"](row, namespace["eval_for_task_once"])
    # The real evaluator reaches its next independent input check. This fixture
    # intentionally supplies no official target list and never runs Docker.
    assert evaluation["status"] == "blocked_missing_eval_spec"
    assert evaluation["attempt_count"] == 0
    assert generation.get("fresh_historical_completion") is None


@pytest.mark.parametrize("workflow", ["single-agent", "validation-council-solve"])
def test_fresh_context_mismatch_is_explicit_at_both_boundaries(monkeypatch, tmp_path, workflow):
    namespace = _configured_namespace(tmp_path, workflow, "local")
    row, generation, observed = _fresh_generation(namespace, monkeypatch, metric_updates={"context_window": None})
    evaluation = namespace["eval_for_task_with_retries"](row, namespace["eval_for_task_once"])
    assert generation["status"] == "technical_generation_identity_failed"
    assert generation["mismatched_identity_fields"] == ["context_window"]
    assert generation["patch_len"] > 0
    prediction, _metric, _pairing = namespace["latest_pair"](
        namespace["base_run_dir"] / row["instance_id"], row["instance_id"]
    )
    assert generation["patch_sha256"] == prediction["patch_sha256"]
    assert evaluation["patch_sha256"] == prediction["patch_sha256"]
    assert observed == []
    assert evaluation["status"] == "technical_generation_identity_failed"
    assert evaluation["reason"] == "generation_runtime_identity_mismatch"
    assert evaluation["mismatched_identity_fields"] == ["context_window"]
    assert evaluation["attempt_count"] == 0


@pytest.mark.parametrize("field,value", [
    ("run_id", "different-run"), ("runtime_tree_sha256", "b" * 64),
    ("model_name", "other-model"), ("workflow", "other-workflow"),
])
def test_real_identity_mismatches_still_block_eval(tmp_path, field, value):
    namespace = _configured_namespace(tmp_path, "single-agent", "local")
    _seed_remote_completed_generation(namespace)
    namespace[field] = value
    result = namespace["eval_for_task_once"]({"instance_id": "task-1"})
    assert result["status"] == "technical_generation_identity_failed"
    assert result["patch_len"] > 0


@pytest.mark.parametrize("change,expected", [
    ("empty", "skipped_no_generation_patch"),
    ("record", "technical_generation_identity_failed"),
    ("proof", "technical_generation_evidence_invalid"),
])
def test_missing_patch_pairing_and_proof_keep_distinct_reasons(tmp_path, change, expected):
    namespace = _configured_namespace(tmp_path, "single-agent", "local")
    _seed_remote_completed_generation(namespace)
    run_dir = namespace["base_run_dir"] / "task-1"
    target = run_dir / ("predictions.jsonl" if change == "empty" else "metrics.jsonl")
    row = json.loads(target.read_text())
    if change == "empty":
        row["model_patch"] = ""
    elif change == "record":
        row["record_id"] = "other-record"
    else:
        row["trusted_patch_extraction"] = {}
    _write_jsonl(target, [row])
    result = namespace["eval_for_task_once"]({"instance_id": "task-1"})
    assert result["status"] == expected
    assert result["reason"]


@pytest.mark.parametrize("unbounded", [False, True])
def test_generator_and_identity_use_one_limit_resolver(monkeypatch, tmp_path, unbounded):
    from opencollab_eval.runtime_config import effective_generation_limits

    value = str(unbounded).lower()
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", value)
    namespace = _remote_namespace(tmp_path, workflow_env={"OPENCOLLAB_UNBOUNDED_LIMITS": value})
    budget, steps = effective_generation_limits(budget=1000, max_steps=3)
    normalized_steps, normalized_budget, _timeout = validate_generation_limits(max_steps=3, budget=1000, timeout=10)
    identity = namespace["generation_runtime_identity"]()
    assert (identity["budget"], identity["max_steps"]) == (budget, steps)
    assert (normalized_budget, normalized_steps) == (budget, steps)


def test_explicit_context_survives_public_runtime_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_CONTEXT_WINDOW", "1048576")
    config = resolve_runtime_config(tmp_path, overrides={"model": "unknown-model", "provider": "openai"})
    assert config["context_window"] == 1_048_576


def test_eval_only_accepts_proven_history_without_rewriting_old_context(tmp_path):
    namespace = _configured_namespace(tmp_path, "single-agent", "local")
    _seed_remote_completed_generation(namespace)
    path = namespace["base_run_dir"] / "task-1" / "metrics.jsonl"
    metric = json.loads(path.read_text())
    metric["context_window"] = None
    _write_jsonl(path, [metric])
    original = path.read_bytes()
    namespace["eval_only"] = True
    result = namespace["eval_for_task_once"]({"instance_id": "task-1"})
    assert result["status"] == "blocked_missing_eval_spec"
    assert path.read_bytes() == original
    assert json.loads(path.read_text())["context_window"] is None


@pytest.mark.parametrize("transport", ["local", "ssh"])
@pytest.mark.parametrize("host,explicit,expected", [
    ("true", None, [None, None]), ("true", "false", [1000, 3]),
    ("false", "true", [None, None]), (None, "true", [None, None]),
])
def test_host_and_explicit_limit_settings_match_the_actual_child(
    monkeypatch, tmp_path, transport, host, explicit, expected,
):
    from opencollab_eval.commands.swe_v1_prolite_common import normalize_workflow_env_entries

    if host is None:
        monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    else:
        monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", host)
    entries = [] if explicit is None else [f"OPENCOLLAB_UNBOUNDED_LIMITS={explicit}"]
    settings = normalize_workflow_env_entries(entries)
    if transport == "ssh":
        # The remote process may start with a different shell environment. The
        # normalized controller payload must retain the caller's effective value.
        monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "false")
    namespace = _remote_namespace(tmp_path, workflow_env=settings, runner_transport=transport)
    environment = dict(os.environ)
    environment.update(namespace["effective_workflow_env"]())
    child = subprocess.run(
        [sys.executable, "-c", "import json; from opencollab_eval.generation.gen_prediction_config "
         "import validate_generation_limits; s,b,t=validate_generation_limits(max_steps=3,budget=1000,timeout=10); "
         "print(json.dumps([b,s]))"],
        env=environment, capture_output=True, text=True, check=True,
    )
    identity = namespace["generation_runtime_identity"]()
    assert json.loads(child.stdout) == expected
    assert [identity["budget"], identity["max_steps"]] == expected
