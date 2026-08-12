from __future__ import annotations

from pathlib import Path

import pytest

from opencollab_eval.commands import swe_g11_parallel_runner as parallel
from opencollab_eval.commands import swe_v1_prolite_runner as standalone


def test_standalone_rejects_checkpointing_before_run_paths(monkeypatch, capsys):
    configured = []
    monkeypatch.setattr(
        standalone,
        "configure_run_paths",
        lambda args: configured.append(args),
    )

    with pytest.raises(SystemExit) as exc:
        standalone.main(argv=["--checkpoint-interval", "30"])

    assert exc.value.code == 2
    assert configured == []
    assert (
        "--checkpoint-interval must be 0 for trusted host extraction"
        in capsys.readouterr().err
    )


def test_standalone_allows_legacy_checkpoint_value_for_eval_only(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        standalone,
        "configure_run_paths",
        lambda _args: (_ for _ in ()).throw(RuntimeError("validation passed")),
    )

    with pytest.raises(RuntimeError, match="validation passed"):
        standalone.main(
            argv=[
                "--eval-only",
                "--parent-output-dir",
                str(tmp_path),
                "--limit",
                "1",
                "--checkpoint-interval",
                "30",
                "--host",
                "worker",
                "--remote-root",
                "/remote/root",
                "--model-name",
                "model",
                "--remote-python",
                "/remote/python",
                "--session-prefix",
                "eval-only",
                "--image-repository",
                "registry.example/swebench",
                "--remote-proxy-base-url",
                "http://127.0.0.1:1",
            ]
        )


def test_parallel_rejects_checkpointing_before_task_start():
    args = parallel.build_parser().parse_args(
        [
            "--start-index",
            "1",
            "--end-index",
            "1",
            "--remote-eval-work-root",
            "/remote/eval",
            "--remote-root",
            "/remote/root",
            "--image-repository",
            "registry.example/swebench",
            "--model-name",
            "model",
            "--llm-model",
            "glm-5.2",
            "--host",
            "worker",
            "--proxy-env-file",
            str(Path("/tmp/proxy.env")),
            "--remote-proxy-base-url",
            "http://127.0.0.1:1",
            "--local-proxy-base-url",
            "http://127.0.0.1:2",
            "--checkpoint-interval",
            "30",
        ]
    )

    with pytest.raises(
        ValueError,
        match="--checkpoint-interval must be 0 for trusted host extraction",
    ):
        parallel.resolve_config(args)
