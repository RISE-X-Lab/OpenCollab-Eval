"""Best-of-N candidate persistence and owned-container recovery."""

from __future__ import annotations

import json

import pytest

from opencollab_eval import safe_files
from opencollab_eval.generation import container_quiescence, gen_prediction_docker
from tests.generation.test_best_of_n_arm import _fake_containers as _fake_containers
from tests.generation.test_best_of_n_arm import _run_main, bon


def test_candidate_is_durable_before_container_retirement(monkeypatch, tmp_path, _fake_containers):
    patch = "diff --git a/a b/a\n+one\n"
    observed = []

    def interrupt_after_retirement(**kwargs):
        saved = kwargs["run_dir"] / "candidate.json"
        record = json.loads(saved.read_text())
        observed.append((record["patch"], record["metrics"]["submission_eligible"]))
        raise SystemExit("simulated process interruption")

    monkeypatch.setattr(bon, "finalize_container_ownership", interrupt_after_retirement)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})
    assert observed == [(patch, True)]
    saved = bon.candidate_run_directory(tmp_path / "out", "task-1", 0) / "candidate.json"
    assert json.loads(saved.read_text())["patch"] == patch


def test_candidate_write_failure_keeps_source_and_records_failure(monkeypatch, tmp_path, _fake_containers):
    retained = []
    quiesced = []
    finalized = []

    def refuse_candidate(path, _payload, **_kwargs):
        raise OSError(f"candidate write failed at {path}")

    monkeypatch.setattr(bon, "write_regular_bytes_atomic", refuse_candidate)
    monkeypatch.setattr(bon, "mark_container_kept", lambda run_dir, cid: retained.append(cid))
    monkeypatch.setattr(bon, "require_container_quiescence", lambda cid: quiesced.append(cid))
    monkeypatch.setattr(bon, "finalize_container_ownership", lambda **kwargs: finalized.append(kwargs))

    with pytest.raises(OSError, match="candidate write failed"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})

    assert retained == ["cid-0"]
    assert quiesced == ["cid-0", "cid-0"]
    assert finalized == []
    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    failure = json.loads((run_dir / "generation_failure.json").read_text())
    assert failure["phase"] == "best-of-n_candidate_0_persistence"
    assert failure["evidence"] == {"container_retained": True, "solver_quiesced": True}


def test_owner_survives_when_both_candidate_write_and_keep_update_fail(monkeypatch, tmp_path, _fake_containers):
    def start_with_owner(_image, name, run_dir):
        gen_prediction_docker.write_container_marker(run_dir, "cid-0", name)
        return "cid-0"

    def fail_write(_path, _payload, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(bon, "start_container_with_marker", start_with_owner)
    monkeypatch.setattr(
        bon,
        "mark_container_preservation_required",
        gen_prediction_docker.mark_container_preservation_required,
    )
    monkeypatch.setattr(bon, "write_regular_bytes_atomic", fail_write)
    monkeypatch.setattr(bon, "mark_container_kept", lambda _run_dir, _cid: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})

    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    owners = list((run_dir / ".opencollab" / "container_owners").glob("*.json"))
    assert len(owners) == 1
    assert json.loads(owners[0].read_text())["state"] == "preservation_required"
    removed = []
    monkeypatch.setattr(gen_prediction_docker, "_owner_is_live", lambda record: False)
    monkeypatch.setattr(gen_prediction_docker, "_remove_owned_container", lambda record: removed.append(record))
    assert gen_prediction_docker.recover_stale_container_owners(run_dir) is False
    assert removed == []


def test_failed_quiescence_freezes_the_retained_source(monkeypatch, tmp_path, _fake_containers):
    def refuse_candidate(_path, _payload, **_kwargs):
        raise OSError("candidate write failed")

    quiescence_calls = []

    def quiesce(cid):
        quiescence_calls.append(cid)
        if len(quiescence_calls) > 1:
            raise RuntimeError("quiescence unproven")

    isolated = []
    monkeypatch.setattr(bon, "write_regular_bytes_atomic", refuse_candidate)
    monkeypatch.setattr(bon, "mark_container_kept", lambda run_dir, cid: None)
    monkeypatch.setattr(bon, "require_container_quiescence", quiesce)
    monkeypatch.setattr(
        bon,
        "isolate_container_for_preservation",
        lambda cid: isolated.append(cid) or "paused",
    )
    with pytest.raises(OSError, match="candidate write failed"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})
    assert isolated == ["cid-0"]
    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    failure = json.loads((run_dir / "generation_failure.json").read_text())
    assert failure["evidence"]["container_isolated"] == "paused"
    assert failure["evidence"]["solver_quiesced"] is False


def test_isolation_pause_is_verified_with_docker_state(monkeypatch):
    calls = []
    states = iter(["true false", "true true"])

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return type("Result", (), {"returncode": 0, "stdout": next(states), "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(container_quiescence.subprocess, "run", fake_run)
    assert container_quiescence.isolate_container_for_preservation("cid-0") == "paused"
    assert [argv[1] for argv in calls] == ["inspect", "pause", "inspect"]


def test_isolation_stops_container_when_pause_is_not_proven(monkeypatch):
    calls = []
    states = iter(["true false", "true false", "false false"])

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return type("Result", (), {"returncode": 0, "stdout": next(states), "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(container_quiescence.subprocess, "run", fake_run)
    assert container_quiescence.isolate_container_for_preservation("cid-0") == "stopped"
    assert [argv[1] for argv in calls] == ["inspect", "pause", "inspect", "stop", "inspect"]


def test_failure_record_error_does_not_mask_candidate_write_error(monkeypatch, tmp_path, _fake_containers):
    def fail_write(_path, _payload, **_kwargs):
        raise OSError("candidate write failed")

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("failure record unavailable")

    monkeypatch.setattr(bon, "write_regular_bytes_atomic", fail_write)
    monkeypatch.setattr(bon, "mark_container_kept", lambda run_dir, cid: None)
    monkeypatch.setattr(bon, "persist_generation_failure", fail_record)
    with pytest.raises(OSError, match="candidate write failed") as observed:
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})
    assert any("failure record unavailable" in note for note in getattr(observed.value, "__notes__", []))


def test_candidate_symlink_inserted_after_start_is_refused_and_source_kept(monkeypatch, tmp_path, _fake_containers):
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("KEEP THIS FILE\n")
    retained = []
    finalized = []

    def insert_symlink(path, payload, **kwargs):
        path.symlink_to(unrelated)
        return safe_files.write_regular_bytes_atomic(path, payload, **kwargs)

    monkeypatch.setattr(bon, "write_regular_bytes_atomic", insert_symlink)
    monkeypatch.setattr(bon, "mark_container_kept", lambda run_dir, cid: retained.append(cid))
    monkeypatch.setattr(bon, "finalize_container_ownership", lambda **kwargs: finalized.append(kwargs))
    with pytest.raises(OSError, match="not a regular file"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})
    assert unrelated.read_text() == "KEEP THIS FILE\n"
    assert retained == ["cid-0"]
    assert finalized == []


def test_cleanup_record_update_failure_preserves_first_saved_patch(monkeypatch, tmp_path, _fake_containers):
    patch = "diff --git a/a b/a\n+one\n"
    writes = []
    finalized = []

    def fail_second_write(path, payload, **kwargs):
        writes.append(path)
        if len(writes) == 2:
            raise OSError("cleanup record update failed")
        return safe_files.write_regular_bytes_atomic(path, payload, **kwargs)

    monkeypatch.setattr(bon, "write_regular_bytes_atomic", fail_second_write)
    monkeypatch.setattr(bon, "finalize_container_ownership", lambda **kwargs: finalized.append(kwargs))
    with pytest.raises(OSError, match="cleanup record update failed"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})
    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    assert len(finalized) == 1
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    assert json.loads((run_dir / "generation_failure.json").read_text())["phase"] == (
        "best-of-n_candidate_0_cleanup_record"
    )


def test_existing_candidate_symlink_is_refused_without_touching_its_target(monkeypatch, tmp_path, _fake_containers):
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("KEEP THIS FILE\n")
    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").symlink_to(unrelated)

    with pytest.raises(OSError, match="not a regular file"):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})

    assert unrelated.read_text() == "KEEP THIS FILE\n"
    assert (run_dir / "candidate.json").is_symlink()
    assert _fake_containers == []


def test_existing_candidate_is_preserved_before_a_fresh_attempt(monkeypatch, tmp_path, _fake_containers):
    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    run_dir.mkdir(parents=True)
    old = {"candidate_index": 0, "patch": "old patch", "metrics": {"workflow_status": "error"}}
    (run_dir / "candidate.json").write_text(json.dumps(old))
    patch = "diff --git a/a b/a\n+new\n"
    _run_main(monkeypatch, tmp_path, {"cid-0": patch, "cid-1": patch, "cid-2": patch})

    assert json.loads((run_dir / "candidate.previous-1.json").read_text()) == old
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch


def test_baseline_cleanup_failure_keeps_the_saved_patch(monkeypatch, tmp_path, _fake_containers):
    def baseline(_cid, _snapshot):
        def fail_cleanup(_self):
            raise RuntimeError("baseline cleanup failed")

        return type("Baseline", (), {"cleanup": fail_cleanup})()

    monkeypatch.setattr(bon, "prepare_trusted_patch_baseline", baseline)
    patch = "diff --git a/a b/a\n+one\n"
    with pytest.raises(RuntimeError, match="baseline cleanup failed"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})

    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    assert json.loads((run_dir / "generation_failure.json").read_text())["phase"] == ("best-of-n_candidate_0_cleanup")


def test_failed_retirement_keeps_candidate_and_blocks_stale_recovery(monkeypatch, tmp_path, _fake_containers):
    def start_with_owner(_image, name, run_dir):
        gen_prediction_docker.write_container_marker(run_dir, "cid-0", name)
        return "cid-0"

    monkeypatch.setattr(bon, "start_container_with_marker", start_with_owner)
    monkeypatch.setattr(
        bon,
        "mark_container_preservation_required",
        gen_prediction_docker.mark_container_preservation_required,
    )
    monkeypatch.setattr(
        gen_prediction_docker,
        "_remove_owned_container",
        lambda record: False,
    )
    monkeypatch.setattr(bon, "finalize_container_ownership", gen_prediction_docker.finalize_container_ownership)
    patch = "diff --git a/a b/a\n+one\n"
    with pytest.raises(RuntimeError, match="ownership marker retained"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})

    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    with pytest.raises(RuntimeError, match="previous candidate and owned container need recovery"):
        _run_main(monkeypatch, tmp_path, {"cid-0": patch})
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    assert list(run_dir.glob("candidate.previous-*.json")) == []


def test_normal_retirement_clears_preserved_owner(monkeypatch, tmp_path, _fake_containers):
    def start_with_owner(_image, name, run_dir):
        gen_prediction_docker.write_container_marker(run_dir, "cid-0", name)
        return "cid-0"

    monkeypatch.setattr(bon, "start_container_with_marker", start_with_owner)
    monkeypatch.setattr(
        bon,
        "mark_container_preservation_required",
        gen_prediction_docker.mark_container_preservation_required,
    )
    monkeypatch.setattr(gen_prediction_docker, "_remove_owned_container", lambda record: True)
    monkeypatch.setattr(bon, "finalize_container_ownership", gen_prediction_docker.finalize_container_ownership)
    patch = "diff --git a/a b/a\n+one\n"
    _run_main(monkeypatch, tmp_path, {"cid-0": patch, "cid-1": patch, "cid-2": patch})

    run_dir = bon.candidate_run_directory(tmp_path / "out", "task-1", 0)
    assert json.loads((run_dir / "candidate.json").read_text())["patch"] == patch
    assert list((run_dir / ".opencollab" / "container_owners").glob("*.json")) == []


def test_preservation_marker_failure_stops_before_solver_runs(monkeypatch, tmp_path, _fake_containers):
    prepared = []
    finalized = []

    def fail_marker(_run_dir, _cid):
        raise OSError("owner marker write failed")

    monkeypatch.setattr(bon, "mark_container_preservation_required", fail_marker)
    monkeypatch.setattr(bon, "prepare_testbed_environment", lambda cid: prepared.append(cid))
    monkeypatch.setattr(bon, "finalize_container_ownership", lambda **kwargs: finalized.append(kwargs))
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, {"cid-0": "diff --git a/a b/a\n+one\n"})

    assert prepared == []
    assert len(finalized) == 3
    failed = bon.candidate_run_directory(tmp_path / "out", "task-1", 0) / "candidate.json"
    assert json.loads(failed.read_text())["metrics"]["error"] == "owner marker write failed"
