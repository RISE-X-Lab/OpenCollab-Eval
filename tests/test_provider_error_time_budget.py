from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest
from swe_v1_prolite_runner_test_support import runner
from test_swe_g11_parallel_runner import _args, _reusable_summary


def _parallel_module():
    module = importlib.import_module(
        "opencollab_eval.commands.swe_g11_parallel_runner"
    )
    return importlib.reload(module)


def _single_runner_args(tmp_path: Path, *, budget: int, workflow_env: list[str]):
    args = [
        "--host", "worker",
        "--remote-root", "/remote/root",
        "--model-name", "model",
        "--session-prefix", "session",
        "--image-repository", "registry.example/swe",
        "--remote-proxy-base-url", "http://worker.invalid",
        "--no-ensure-remote-proxy",
        "--provider-error-time-budget", str(budget),
        "--json-output", str(tmp_path / "report.json"),
        "--markdown-output", str(tmp_path / "report.md"),
        "--dry-run",
    ]
    for item in workflow_env:
        args.extend(("--workflow-env", item))
    return args


def test_parallel_budget_is_recorded_and_forwarded():
    module = _parallel_module()
    config = module.resolve_config(
        _args(
            provider_error_time_budget=120,
            workflow_env=["OPENCOLLAB_WIRE_PROTOCOL=responses"],
        )
    )

    command = module.task_command(config, 51)

    assert config.provider_error_time_budget == 120
    assert "OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=120" in config.workflow_env
    assert "OPENCOLLAB_LLM_MAX_RETRIES=32" in config.workflow_env
    assert command[command.index("--provider-error-time-budget") + 1] == "120"
    summary = module.aggregate(config, [])
    assert summary["effective_timeouts"] == {
        "llm_normal": 900,
        "llm_wall": 1020,
        "generation": 14520,
        "task_wall": 15420,
        "controller": 240120,
        "official_eval": 7200,
    }


def test_parallel_markdown_records_provider_budget(tmp_path: Path):
    module = _parallel_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            provider_error_time_budget=120,
            workflow_env=["OPENCOLLAB_WIRE_PROTOCOL=responses"],
        )
    )
    module.write_markdown(config, module.aggregate(config, []))

    markdown = (tmp_path / "parallel_summary.md").read_text(encoding="utf-8")
    assert "provider_error_time_budget: `120`" in markdown
    assert "llm_normal_timeout: `900`" in markdown
    assert "llm_wall_timeout: `1020`" in markdown
    assert "generation_timeout: `14520`" in markdown
    assert "official_eval_timeout: `7200`" in markdown


def test_reuse_requires_matching_provider_error_budget_identity():
    module = _parallel_module()
    config = replace(
        module.resolve_config(_args(provider_error_time_budget=120)),
        runtime_tree_sha256="a" * 64,
    )
    summary = _reusable_summary(config, 51)
    summary["workflow_env"] = dict(
        item.split("=", 1) for item in config.workflow_env
    )

    assert module.report_is_reusable(summary, config, 51) is True
    summary["workflow_env"].pop("OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET")
    assert module.report_is_reusable(summary, config, 51) is False


def test_parallel_budget_rejects_negative_or_conflicting_identity():
    module = _parallel_module()
    with pytest.raises(ValueError, match="non-negative"):
        module.resolve_config(_args(provider_error_time_budget=-1))
    with pytest.raises(ValueError, match="must match"):
        module.resolve_config(
            _args(
                provider_error_time_budget=120,
                workflow_env=["OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=60"],
            )
        )


def test_responses_retry_count_preserves_zero_budget_and_caps_reserved_mode():
    module = _parallel_module()
    zero_budget = module.resolve_config(
        _args(workflow_env=["OPENCOLLAB_WIRE_PROTOCOL=responses"])
    )
    assert "OPENCOLLAB_LLM_MAX_RETRIES=3" in zero_budget.workflow_env

    with pytest.raises(ValueError, match="between 0 and 32"):
        module.resolve_config(
            _args(
                provider_error_time_budget=120,
                workflow_env=[
                    "OPENCOLLAB_WIRE_PROTOCOL=responses",
                    "OPENCOLLAB_LLM_MAX_RETRIES=10000",
                ],
            )
        )


@pytest.mark.parametrize(
    ("budget", "configured", "expected"),
    [(0, None, "3"), (120, None, "32"), (120, "32", "32")],
)
def test_single_runner_normalizes_responses_retry_count(
    monkeypatch,
    tmp_path: Path,
    budget: int,
    configured: str | None,
    expected: str,
):
    captured = {}
    monkeypatch.setattr(runner, "configure_run_paths", lambda _args: None)
    monkeypatch.setattr(runner, "write_local_report", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "run_remote",
        lambda args: captured.update(vars(args)) or {"status": "dry_run"},
    )
    workflow_env = ["OPENCOLLAB_WIRE_PROTOCOL=responses"]
    if configured is not None:
        workflow_env.append(f"OPENCOLLAB_LLM_MAX_RETRIES={configured}")

    assert runner.main(
        argv=_single_runner_args(
            tmp_path,
            budget=budget,
            workflow_env=workflow_env,
        )
    ) == 0
    assert f"OPENCOLLAB_LLM_MAX_RETRIES={expected}" in captured["workflow_env"]


def test_single_runner_rejects_unbounded_responses_retry_count(tmp_path: Path):
    with pytest.raises(SystemExit, match="2"):
        runner.main(
            argv=_single_runner_args(
                tmp_path,
                budget=120,
                workflow_env=[
                    "OPENCOLLAB_WIRE_PROTOCOL=responses",
                    "OPENCOLLAB_LLM_MAX_RETRIES=10000",
                ],
            )
        )


def test_single_runner_extends_generation_but_not_normal_llm_or_eval(
    monkeypatch,
    tmp_path: Path,
):
    captured = {}
    reports = []
    monkeypatch.setattr(runner, "configure_run_paths", lambda _args: None)
    monkeypatch.setattr(
        runner,
        "write_local_report",
        lambda summary, *_args: reports.append(dict(summary)),
    )

    def fake_run_remote(args):
        captured.update(vars(args))
        return {"status": "dry_run", "markdown": "# Task report\n"}

    monkeypatch.setattr(runner, "run_remote", fake_run_remote)
    exit_code = runner.main(
        argv=[
            "--host", "worker",
            "--remote-root", "/remote/root",
            "--model-name", "model",
            "--session-prefix", "session",
            "--image-repository", "registry.example/swe",
            "--remote-proxy-base-url", "http://worker.invalid",
            "--no-ensure-remote-proxy",
            "--llm-timeout", "60",
            "--provider-error-time-budget", "120",
            "--swe-timeout", "300",
            "--task-wall-timeout", "400",
            "--total-timeout", "500",
            "--eval-timeout", "90",
            "--json-output", str(tmp_path / "report.json"),
            "--markdown-output", str(tmp_path / "report.md"),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert captured["llm_timeout"] == 60
    assert captured["swe_timeout"] == 420
    assert captured["task_wall_timeout"] == 520
    assert captured["total_timeout"] == 620
    assert captured["eval_timeout"] == 90
    assert "OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=120" in captured["workflow_env"]
    assert reports[-1]["provider_time_budget"]["effective"] == {
        "llm_normal": 60,
        "llm_wall": 180,
        "generation": 420,
        "task_wall": 520,
        "controller": 620,
        "official_eval": 90,
    }
    assert "Normal LLM timeout `60` seconds" in reports[-1]["markdown"]
    assert "Official evaluation timeout `90` seconds" in reports[-1]["markdown"]
    assert len(reports) == 1
