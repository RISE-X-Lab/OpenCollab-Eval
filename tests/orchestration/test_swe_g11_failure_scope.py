from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencollab_eval.commands import _swe_g11_config as config_module
from opencollab_eval.commands import swe_g11_parallel_runner as runner


@pytest.mark.parametrize("scope", ("task", "image", ""))
def test_local_failures_never_pause_the_batch(scope: str) -> None:
    result = {
        "completed": False,
        "technical_failed": 1,
        "failure_scope": scope,
        "error": "docker ssh timeout connection refused",
    }
    assert config_module.systemic_failure_reasons(result) == []
    assert config_module.result_resource_reasons(result) == []


def test_claimed_shared_scope_requires_direct_failed_probe() -> None:
    result = {
        "failure_scope": "shared_infrastructure",
        "failure_probe": {"direct": False, "status": "failed"},
    }
    assert config_module.systemic_failure_reasons(result) == []
    result["failure_probe"] = {"direct": True, "status": "passed"}
    assert config_module.systemic_failure_reasons(result) == []
    result["failure_probe"] = {"direct": True, "status": "failed"}
    assert config_module.systemic_failure_reasons(result) == [
        "shared_infrastructure_probe_failed"
    ]


def test_unquiesced_generation_is_a_structured_batch_pause_signal() -> None:
    result = {
        "failure_scope": "task",
        "rows": [
            {
                "task": "task-7",
                "generation": {
                    "status": "technical_failed",
                    "execution_quiesced": False,
                },
            }
        ],
    }

    assert config_module.result_resource_reasons(result) == [
        "generation_execution_not_quiesced"
    ]
    assert config_module.systemic_failure_reasons(result) == [
        "generation_execution_not_quiesced"
    ]


def test_task_failure_stays_local_when_public_probes_pass(monkeypatch, tmp_path) -> None:
    config = SimpleNamespace(skip_health_checks=False, dry_run=False, output_dir=tmp_path)
    monkeypatch.setattr(
        runner,
        "run_remote_health_checks",
        lambda _config: {"direct": True, "status": "ok"},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_model_probe",
        lambda _config: {"status": "ok"},
    )
    result = runner.confirm_shared_runtime_after_task_failure(
        config,
        {"completed": True, "technical_failed": 1, "failure_scope": "image"},
    )
    assert result["failure_scope"] == "image"
    assert result["failure_probe"]["status"] == "passed"


def test_empty_patch_receives_a_direct_model_probe(monkeypatch, tmp_path) -> None:
    config = SimpleNamespace(skip_health_checks=False, dry_run=False, output_dir=tmp_path)
    calls = []
    monkeypatch.setattr(
        runner,
        "run_remote_health_checks",
        lambda _config: {"direct": True, "status": "ok"},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_model_probe",
        lambda _config: calls.append("model") or {"status": "ok"},
    )

    result = runner.confirm_shared_runtime_after_task_failure(
        config,
        {"completed": True, "technical_failed": 0, "empty_patch": 1},
    )

    assert calls == ["model"]
    assert result["failure_scope"] == "task"
    assert result["failure_probe"]["status"] == "passed"


def test_empty_patch_probe_failure_can_pause_the_batch(monkeypatch, tmp_path) -> None:
    config = SimpleNamespace(skip_health_checks=False, dry_run=False, output_dir=tmp_path)
    failure = runner._shared_health.SharedProbeFailure(
        "provider rejected request",
        {"status": "failed", "direct": True, "http_status": 403},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_health_checks",
        lambda _config: {"direct": True, "status": "ok"},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_model_probe",
        lambda _config: (_ for _ in ()).throw(failure),
    )

    result = runner.confirm_shared_runtime_after_task_failure(
        config,
        {"completed": True, "technical_failed": 0, "empty_patch": 1},
    )

    assert result["failure_scope"] == "shared_infrastructure"
    assert result["failure_probe"]["status"] == "failed"
    assert config_module.systemic_failure_reasons(result) == [
        "shared_infrastructure_probe_failed"
    ]


def test_direct_public_probe_failure_is_the_batch_pause_signal(monkeypatch, tmp_path) -> None:
    config = SimpleNamespace(skip_health_checks=False, dry_run=False, output_dir=tmp_path)
    failure = runner._shared_health.SharedProbeFailure(
        "shared storage failed",
        {"status": "failed", "direct": True},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_health_checks",
        lambda _config: (_ for _ in ()).throw(failure),
    )
    result = runner.confirm_shared_runtime_after_task_failure(
        config,
        {"completed": False, "technical_failed": 1},
    )
    assert result["failure_scope"] == "shared_infrastructure"
    assert result["failure_probe"]["direct"] is True
    assert config_module.systemic_failure_reasons(result) == [
        "shared_infrastructure_probe_failed"
    ]


@pytest.mark.parametrize(
    "error",
    (
        ValueError("malformed thinking JSON"),
        OSError("token file is unreadable"),
        OSError("subprocess could not be started"),
    ),
)
def test_probe_setup_errors_do_not_pause_the_batch(monkeypatch, tmp_path, error) -> None:
    config = SimpleNamespace(skip_health_checks=False, dry_run=False, output_dir=tmp_path)
    monkeypatch.setattr(
        runner,
        "run_remote_health_checks",
        lambda _config: {"direct": True, "status": "ok"},
    )
    monkeypatch.setattr(
        runner,
        "run_remote_model_probe",
        lambda _config: (_ for _ in ()).throw(error),
    )

    result = runner.confirm_shared_runtime_after_task_failure(
        config,
        {"completed": False, "technical_failed": 1, "failure_scope": "image"},
    )

    assert result["failure_scope"] == "image"
    assert result["failure_probe"]["direct"] is False
    assert config_module.systemic_failure_reasons(result) == []
