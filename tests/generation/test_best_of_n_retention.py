"""Failure boundaries for Best-of-N candidate source retention."""

from __future__ import annotations

import asyncio
import gc
import json
import tempfile
from pathlib import Path

import pytest

from opencollab_eval.generation import (
    candidate_retention,
    gen_prediction_docker,
    gen_prediction_patch,
)
from opencollab_eval.generation import gen_prediction as gp
from opencollab_eval.generation import gen_prediction_best_of_n as bon
from tests.generation.test_best_of_n_arm import _SNAPSHOT, _run_main


@pytest.fixture
def owned_candidate(monkeypatch, tmp_path):
    baselines: list[Path] = []
    retired: list[str] = []
    workspace = tmp_path / "solver-workspace.patch"

    def start(_image, name, run_dir):
        gen_prediction_docker.write_container_marker(run_dir, "cid-0", name)
        return "cid-0"

    def baseline(_cid, snapshot):
        temporary = tempfile.TemporaryDirectory(prefix="bon-test-baseline-")
        root = Path(temporary.name)
        (root / "trusted-before-solver").write_text("baseline", encoding="utf-8")
        baselines.append(root)
        return gen_prediction_patch.TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=temporary,
            git_dir=root / "repo.git",
            archive_sha256="a" * 64,
            archive_bytes=1,
            archive_entries=1,
            extracted_bytes=1,
        )

    def remove(record):
        retired.append(record["container_id"])
        workspace.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(bon, "start_container_with_marker", start)
    monkeypatch.setattr(
        bon, "mark_container_preservation_required", gen_prediction_docker.mark_container_preservation_required
    )
    monkeypatch.setattr(bon, "finalize_container_ownership", gen_prediction_docker.finalize_container_ownership)
    monkeypatch.setattr(bon, "prepare_testbed_environment", lambda _cid: None)
    monkeypatch.setattr(bon, "stash_solver_runtime_dependencies", lambda *_args: object())
    monkeypatch.setattr(bon, "restore_solver_runtime_dependencies", lambda *_args: None)
    monkeypatch.setattr(bon, "remove_solver_runtime_dependencies", lambda *_args: None)
    monkeypatch.setattr(bon, "container_image_id", lambda _cid: "sha256:" + "8" * 64)
    monkeypatch.setattr(bon, "prepare_solver_git_snapshot", lambda _cid, _base: _SNAPSHOT)
    monkeypatch.setattr(bon, "prepare_trusted_patch_baseline", baseline)
    monkeypatch.setattr(bon, "require_container_quiescence", lambda _cid: None)
    monkeypatch.setattr(bon, "run_with_bounded_shutdown", lambda awaitable: asyncio.run(awaitable))
    monkeypatch.setattr(gp, "_container_owner_label_state", lambda _cid, _token: "matching")
    monkeypatch.setattr(gen_prediction_docker, "_remove_owned_container", remove)
    return baselines, retired, workspace


def _candidate_dir(tmp_path: Path) -> Path:
    return bon.candidate_run_directory(tmp_path / "out", "task-1", 0)


def _owner_record(run_dir: Path) -> dict:
    paths = list((run_dir / ".opencollab" / "container_owners").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _recovery(run_dir: Path) -> dict:
    paths = list((run_dir / "candidate-recovery").glob("*/recovery.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_first_candidate_write_failure_retains_trusted_baseline(monkeypatch, tmp_path, owned_candidate):
    baselines, retired, _workspace = owned_candidate
    monkeypatch.setattr(
        bon,
        "write_regular_bytes_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+work\n"})
    gc.collect()

    run_dir = _candidate_dir(tmp_path)
    receipt = _recovery(run_dir)
    assert not baselines[0].exists()
    assert (Path(receipt["baseline"]["git_dir"]).parent / "trusted-before-solver").read_text() == "baseline"
    assert receipt["reason"] == "candidate_record_unwritten"
    assert receipt["patch_extraction_succeeded"] is True
    assert _owner_record(run_dir)["state"] == "kept"
    assert retired == []
    failure = json.loads((run_dir / "generation_failure.json").read_text(encoding="utf-8"))
    assert failure["evidence"]["container_retained"] is True
    assert failure["evidence"]["candidate_recovery_path"]

    def recover(_cid, baseline):
        assert baseline.git_dir == Path(receipt["baseline"]["git_dir"])
        assert (baseline.git_dir.parent / "trusted-before-solver").read_text() == "baseline"
        return "diff --git a/a b/a\n+recovered\n", [], {"candidate_tree": "a" * 40}

    monkeypatch.setattr(gp, "_owner_is_live", lambda _record: False)
    monkeypatch.setattr(gen_prediction_patch, "extract_patch_guarded", recover)
    result = candidate_retention.recover_candidate(
        Path(receipt["recovery_environment"]["PYTHONPATH"]),
        Path(failure["evidence"]["candidate_recovery_path"]),
    )
    assert Path(result["patch_path"]).read_text() == "diff --git a/a b/a\n+recovered\n"
    assert result["model_calls"] == 0


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt("stopped"), SystemExit("stopped"), asyncio.CancelledError("stopped")]
)
def test_interruption_preserves_unextracted_workspace(monkeypatch, tmp_path, owned_candidate, interruption):
    baselines, retired, workspace = owned_candidate

    def interrupt(awaitable):
        awaitable.close()
        workspace.write_text("diff --git a/a b/a\n+in-progress\n", encoding="utf-8")
        raise interruption

    monkeypatch.setattr(bon, "run_with_bounded_shutdown", interrupt)
    with pytest.raises(type(interruption), match="stopped"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+work\n"})
    gc.collect()

    run_dir = _candidate_dir(tmp_path)
    receipt = _recovery(run_dir)
    assert workspace.exists()
    assert retired == []
    assert _owner_record(run_dir)["state"] == "kept"
    assert receipt["reason"] == "trusted_patch_extraction_incomplete"
    assert not baselines[0].exists()
    assert (Path(receipt["baseline"]["git_dir"]).parent / "trusted-before-solver").read_text() == "baseline"
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    assert candidate["patch"] == ""
    assert candidate["metrics"]["candidate_recovery_path"]
    assert candidate["metrics"]["submission_eligible"] is False


def test_interrupt_and_candidate_write_failure_preserve_original_error(monkeypatch, tmp_path, owned_candidate):
    _baselines, retired, workspace = owned_candidate

    def interrupt(awaitable):
        awaitable.close()
        workspace.write_text("in-progress", encoding="utf-8")
        raise KeyboardInterrupt("stopped")

    monkeypatch.setattr(bon, "run_with_bounded_shutdown", interrupt)
    monkeypatch.setattr(
        bon,
        "write_regular_bytes_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(KeyboardInterrupt, match="stopped") as captured:
        _run_main(monkeypatch, tmp_path, {"cid-0": "unused"})

    run_dir = _candidate_dir(tmp_path)
    assert "candidate record could not be written" in " ".join(captured.value.__notes__)
    assert workspace.exists()
    assert retired == []
    assert _owner_record(run_dir)["state"] == "kept"
    assert Path(_recovery(run_dir)["baseline"]["git_dir"]).parent.exists()


def test_interrupt_after_extraction_preserves_unwritten_candidate_source(monkeypatch, tmp_path, owned_candidate):
    baselines, retired, _workspace = owned_candidate

    def interrupt_after_extraction(_metrics, _patch):
        raise KeyboardInterrupt("after extraction")

    monkeypatch.setattr(bon, "normalize_trusted_extraction_status", interrupt_after_extraction)
    patch = "diff --git a/a b/a\n+work\n"
    with pytest.raises(KeyboardInterrupt, match="after extraction"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})

    run_dir = _candidate_dir(tmp_path)
    receipt = _recovery(run_dir)
    assert receipt["reason"] == "candidate_record_unwritten"
    assert (Path(receipt["baseline"]["git_dir"]).parent / "trusted-before-solver").read_text() == "baseline"
    assert not baselines[0].exists()
    assert _owner_record(run_dir)["state"] == "kept"
    assert retired == []
    candidate = json.loads((run_dir / "candidate.json").read_text())
    assert candidate["patch"] == patch
    assert candidate["metrics"]["submission_eligible"] is False


def test_pre_extraction_exception_retains_modified_workspace_and_baseline(monkeypatch, tmp_path, owned_candidate):
    baselines, retired, workspace = owned_candidate

    def preparation_fails(_cid, _runtime):
        workspace.write_text("diff --git a/a b/a\n+in-progress\n", encoding="utf-8")
        raise RuntimeError("pre-extraction step stopped")

    monkeypatch.setattr(bon, "remove_solver_runtime_dependencies", preparation_fails)
    with pytest.raises(SystemExit) as stopped:
        _run_main(monkeypatch, tmp_path, {"cid-0": "unused"})
    assert stopped.value.code == 1

    run_dir = _candidate_dir(tmp_path)
    receipt = _recovery(run_dir)
    assert receipt["reason"] == "trusted_patch_extraction_incomplete"
    assert workspace.exists()
    assert _owner_record(run_dir)["state"] == "kept"
    assert (Path(receipt["baseline"]["git_dir"]).parent / "trusted-before-solver").read_text() == "baseline"
    assert all(not path.exists() for path in baselines)
    assert retired == []
    candidate = json.loads((run_dir / "candidate.json").read_text())
    assert candidate["metrics"]["error_type"] == "RuntimeError"
    assert candidate["metrics"]["candidate_recovery_path"]


def test_receipt_write_failure_keeps_baseline_and_owned_source(monkeypatch, tmp_path, owned_candidate):
    _baselines, retired, _workspace = owned_candidate
    monkeypatch.setattr(
        bon,
        "write_regular_bytes_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("candidate disk full")),
    )
    monkeypatch.setattr(
        candidate_retention,
        "_write_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt disk full")),
    )

    with pytest.raises(OSError, match="candidate disk full"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+work\n"})

    run_dir = _candidate_dir(tmp_path)
    baseline = list((run_dir / "candidate-recovery").glob("*/trusted-baseline/trusted-before-solver"))
    assert len(baseline) == 1
    assert baseline[0].read_text(encoding="utf-8") == "baseline"
    assert _owner_record(run_dir)["state"] == "kept"
    assert retired == []


def test_normal_candidate_retires_source_and_cleans_temporary_baseline(monkeypatch, tmp_path, owned_candidate):
    baselines, retired, _workspace = owned_candidate
    patch = "diff --git a/a b/a\n+work\n"

    _run_main(monkeypatch, tmp_path, {"cid-0": patch, "cid-1": patch, "cid-2": patch})

    assert retired == ["cid-0"] * 3
    assert all(not path.exists() for path in baselines)
    assert len(baselines) == 3
    assert json.loads((_candidate_dir(tmp_path) / "candidate.json").read_text())["patch"] == patch


def test_second_candidate_write_failure_keeps_first_record(monkeypatch, tmp_path, owned_candidate):
    baselines, retired, _workspace = owned_candidate
    original = bon.write_regular_bytes_atomic
    candidate_writes = 0

    def fail_second(path, data, **kwargs):
        nonlocal candidate_writes
        if path.name == "candidate.json":
            candidate_writes += 1
            if candidate_writes == 2:
                raise OSError("second write failed")
        return original(path, data, **kwargs)

    monkeypatch.setattr(bon, "write_regular_bytes_atomic", fail_second)
    patch = "diff --git a/a b/a\n+work\n"
    with pytest.raises(OSError, match="second write failed"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})

    run_dir = _candidate_dir(tmp_path)
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    assert retired == ["cid-0"]
    assert not baselines[0].exists()
    assert not (run_dir / "candidate-recovery").exists()
