from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from gen_prediction_openhands_support import install_fake_openhands_process

from opencollab_eval.generation import gen_prediction_openhands as gpo
from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot


def test_main_persists_delayed_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_file = tmp_path / "instance.json"
    output = tmp_path / "predictions.jsonl"
    instance_file.write_text(
        json.dumps(
            {
                "instance_id": "acme__widget-1",
                "base_commit": "b" * 40,
                "repo": "acme/widget",
                "problem_statement": "Fix the widget.",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gpo.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "container-123",
    )
    monkeypatch.setattr(
        gpo.container_guard,
        "container_image_id",
        lambda container_id: "sha256:" + "8" * 64,
    )
    snapshot = SolverGitSnapshot(
        anonymous_head="c" * 40,
        base_tree="d" * 40,
        commit_count=1,
        remote_count=0,
        extra_git_metadata=0,
        removed_git_metadata=0,
    )
    monkeypatch.setattr(gpo, "anonymous_solver_task_id", lambda: "solver-" + "a" * 32)
    monkeypatch.setattr(
        gpo,
        "prepare_solver_git_snapshot",
        lambda cid, base: snapshot,
    )
    monkeypatch.setattr(
        gpo,
        "prepare_trusted_patch_baseline",
        lambda cid, prepared_snapshot: SimpleNamespace(cleanup=lambda: None),
    )
    monkeypatch.setattr(
        gpo,
        "_run_openhands",
        lambda **kwargs: {
            "status": "openhands_cleanup_failed",
            "returncode": 125,
            "execution_quiesced": False,
            "host_execution_quiesced": True,
            "container_execution_quiesced": False,
            "container_quiescence_returncode": 125,
            "container_quiescence_error": "busy",
            "external_container_cleanup": {"proven": True},
        },
    )
    monkeypatch.setattr(gpo.gp, "finalize_container_ownership", lambda **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction_openhands.py",
            "--instance-file",
            str(instance_file),
            "--output",
            str(output),
            "--command",
            "solver {container_id} {prompt_file} {output_dir}",
        ],
    )

    with pytest.raises(RuntimeError, match="execution cleanup did not quiesce"):
        gpo.main()

    failure = json.loads((tmp_path / "generation_failure.json").read_text())
    assert failure["phase"] == "openhands_generation"
    assert failure["evidence"]["host_execution_quiesced"] is True
    assert failure["evidence"]["container_execution_quiesced"] is False
    assert failure["evidence"]["container_quiescence_error"] == "busy"
    assert failure["evidence"]["external_container_cleanup"] == {"proven": True}
    assert not output.exists()


def test_supervisor_reap_failure_still_cleans_external_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    install_fake_openhands_process(
        monkeypatch,
        times_out=True,
        captured=captured,
    )
    monkeypatch.setattr(
        gpo.container_guard,
        "terminate_supervisor_process",
        lambda process: "supervisor process did not exit after SIGKILL",
    )
    cleanups: list[Path] = []
    monkeypatch.setattr(
        gpo,
        "cleanup_external_solver_containers",
        lambda output_dir: cleanups.append(output_dir) or {"proven": True},
    )

    result = gpo._run_openhands(
        command_template="solver {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "solver-" + "a" * 32},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=0.1,
    )

    assert cleanups == [tmp_path / "output"]
    assert result["status"] == "openhands_cleanup_failed"
    assert result["host_execution_quiesced"] is False
    assert result["host_supervisor_cleanup_error"] == (
        "supervisor process did not exit after SIGKILL"
    )


def test_openhands_finalization_preserves_generation_error_and_reaches_container_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_error = RuntimeError("trusted baseline cleanup failed")
    evidence_error = OSError("evidence copy failed")
    generation_error = ValueError("OpenHands generation failed")
    baseline = SimpleNamespace(cleanup=lambda: (_ for _ in ()).throw(baseline_error))
    finalize_calls: list[dict] = []
    metrics: dict = {}

    monkeypatch.setattr(
        gpo.shutil,
        "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(evidence_error),
    )
    monkeypatch.setattr(
        gpo.gp,
        "output_staging_requires_container_preservation",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        gpo.gp,
        "finalize_container_ownership",
        lambda **kwargs: finalize_calls.append(kwargs),
    )

    openhands_dir = tmp_path / "attempt"
    openhands_dir.mkdir()
    returned = gpo._cleanup_openhands_attempt(
        trusted_baseline=baseline,
        evidence_dir=tmp_path / "evidence" / "attempt",
        openhands_dir=openhands_dir,
        run_dir=tmp_path,
        cid="container-123",
        name="oc-oh-test",
        pending_required=False,
        pending_path=None,
        metrics=metrics,
        patch="",
        generation_error=generation_error,
        keep_container=False,
    )

    assert returned is generation_error
    assert [call["cid"] for call in finalize_calls] == ["container-123"]
    assert finalize_calls[0]["completed"] is False
    assert any("trusted baseline cleanup failed" in item for item in metrics["cleanup_errors"])
    assert any("evidence copy failed" in item for item in metrics["cleanup_errors"])
    notes = getattr(generation_error, "__notes__", [])
    assert any("trusted baseline cleanup failed" in note for note in notes)
    assert any("evidence copy failed" in note for note in notes)


def test_openhands_finalization_returns_cleanup_error_after_attempting_all_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_error = RuntimeError("baseline cleanup failed")
    evidence_error = OSError("evidence copy failed")
    finalize_error = PermissionError("container finalization failed")
    baseline = SimpleNamespace(cleanup=lambda: (_ for _ in ()).throw(baseline_error))
    finalize_calls: list[dict] = []
    metrics: dict = {}

    monkeypatch.setattr(
        gpo.shutil,
        "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(evidence_error),
    )
    monkeypatch.setattr(
        gpo.gp,
        "output_staging_requires_container_preservation",
        lambda *_args, **_kwargs: False,
    )

    def fail_finalize(**kwargs):
        finalize_calls.append(kwargs)
        raise finalize_error

    monkeypatch.setattr(gpo.gp, "finalize_container_ownership", fail_finalize)

    openhands_dir = tmp_path / "attempt"
    openhands_dir.mkdir()
    returned = gpo._cleanup_openhands_attempt(
        trusted_baseline=baseline,
        evidence_dir=tmp_path / "evidence" / "attempt",
        openhands_dir=openhands_dir,
        run_dir=tmp_path,
        cid="container-123",
        name="oc-oh-test",
        pending_required=False,
        pending_path=None,
        metrics=metrics,
        patch="",
        generation_error=None,
        keep_container=False,
    )

    assert returned is baseline_error
    assert [call["cid"] for call in finalize_calls] == ["container-123"]
    assert finalize_calls[0]["completed"] is False
    assert len(metrics["cleanup_errors"]) == 3
    notes = getattr(baseline_error, "__notes__", [])
    assert any("evidence copy failed" in note for note in notes)
    assert any("container finalization failed" in note for note in notes)
