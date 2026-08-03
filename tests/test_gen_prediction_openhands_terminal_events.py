from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from gen_prediction_openhands_support import (
    install_fake_openhands_process,
    write_openhands_state,
)

from opencollab_eval.engine.swe_eval_decision import TaskSnapshot, TaskState, decide_task
from opencollab_eval.generation import gen_prediction_openhands as gpo
from opencollab_eval.generation import openhands_events
from opencollab_eval.generation.gen_prediction_patch import TrustedPatchExtraction
from opencollab_eval.generation.gen_prediction_snapshot import SolverGitSnapshot


def _terminal_event() -> dict[str, str]:
    return {
        "kind": "ConversationErrorEvent",
        "code": "LLMBadRequestError",
        "detail": "provider configuration is invalid",
    }


def _install_main_doubles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch: str,
) -> tuple[Path, Path]:
    instance_file = tmp_path / "instance.json"
    output = tmp_path / "predictions.jsonl"
    metrics = tmp_path / "metrics.jsonl"
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
    monkeypatch.setattr(gpo, "anonymous_solver_task_id", lambda: "solver-" + "a" * 32)
    snapshot = SolverGitSnapshot(
        anonymous_head="c" * 40,
        base_tree="d" * 40,
        commit_count=1,
        remote_count=0,
        extra_git_metadata=0,
        removed_git_metadata=0,
    )
    monkeypatch.setattr(gpo, "prepare_solver_git_snapshot", lambda cid, base: snapshot)
    baseline = SimpleNamespace(snapshot=snapshot, cleanup=lambda: None)
    monkeypatch.setattr(
        gpo,
        "prepare_trusted_patch_baseline",
        lambda cid, prepared_snapshot: baseline,
    )
    proof = TrustedPatchExtraction(
        fixed_anonymous_base=snapshot.anonymous_head,
        base_tree=snapshot.base_tree,
        baseline_archive_sha256="e" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="f" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        patch_bytes=len(patch.encode()),
        candidate_tree="a" * 40,
        changed_paths=(),
        path_modes=(),
    ).as_dict()
    monkeypatch.setattr(
        gpo,
        "extract_patch_guarded",
        lambda cid, trusted_baseline, **kwargs: (patch, [], proof),
    )
    def run_openhands(**kwargs):
        write_openhands_state(kwargs["output_dir"])
        return {
            "status": "done",
            "returncode": 0,
            "duration_s": 1.0,
            "execution_quiesced": True,
            "host_execution_quiesced": True,
            "container_execution_quiesced": True,
            "openhands_terminal_error": _terminal_event(),
        }

    monkeypatch.setattr(gpo, "_run_openhands", run_openhands)
    pending: dict[str, object] = {}

    def persist_pending_output(**kwargs):
        pending.update(kwargs)
        return tmp_path / "pending-output.json"

    def publish_pending_output(run_dir, path):
        gpo.gp.append_output_records(
            pending["predictions_path"],
            pending["metrics_path"],
            pending["prediction"],
            pending["metric"],
        )
        return "published"

    monkeypatch.setattr(gpo.gp, "persist_pending_output", persist_pending_output)
    monkeypatch.setattr(gpo.gp, "publish_pending_output", publish_pending_output)
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
            "--metrics",
            str(metrics),
            "--command",
            "openhands --headless --json --file {prompt_file}",
            "--llm-model",
            "provider/model",
        ],
    )
    return output, metrics


def test_run_openhands_records_zero_exit_terminal_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _terminal_event()
    install_fake_openhands_process(
        monkeypatch,
        stdout=json.dumps(event) + "\nAgent finished\n",
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --json --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "done"
    assert result["returncode"] == 0
    assert result["openhands_terminal_error"] == event


def test_non_error_terminal_event_and_iteration_limit_remain_eligible(tmp_path: Path) -> None:
    stdout = tmp_path / "openhands.stdout.log"
    stdout.write_text(
        json.dumps({"kind": "MessageEvent", "source": "agent"}) + "\n",
        encoding="utf-8",
    )
    assert openhands_events.terminal_error_evidence(stdout) == {}
    stdout.write_text(
        json.dumps(
            {
                "kind": "ConversationErrorEvent",
                "code": "MaxIterationsReached",
                "detail": "Agent reached maximum iterations limit (120).",
            }
        )
        + "\nAgent finished\n",
        encoding="utf-8",
    )
    assert openhands_events.terminal_error_evidence(stdout) == {}


def test_main_turns_zero_exit_error_without_patch_into_technical_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, metrics = _install_main_doubles(tmp_path, monkeypatch, patch="")

    with pytest.raises(RuntimeError, match="LLMBadRequestError"):
        gpo.main()

    assert not output.exists()
    assert not metrics.exists()
    failure = json.loads((tmp_path / "generation_failure.json").read_text())
    assert failure["phase"] == "openhands_generation"
    assert failure["failure_scope"] == "task"
    assert failure["evidence"]["workflow_status"] == "error"
    assert failure["evidence"]["openhands_terminal_error"] == _terminal_event()


def test_main_keeps_nonempty_candidate_from_terminal_error_eligible_for_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = "diff --git a/widget.py b/widget.py\n+fixed = True\n"
    output, metrics_path = _install_main_doubles(tmp_path, monkeypatch, patch=patch)

    gpo.main()

    prediction = json.loads(output.read_text())
    metric = json.loads(metrics_path.read_text())
    decision = decide_task(
        TaskSnapshot(
            task_id="acme__widget-1",
            prediction=prediction,
            metric=metric,
            metric_pairing="record_id_patch_sha_match",
        )
    )
    assert metric["openhands_terminal_error"] == _terminal_event()
    assert metric["workflow_status"] == "done"
    assert decision.state is TaskState.READY_FOR_EVAL
    assert decision.ready_for_eval is True
