"""Transport failures enter stop fallback and retain confirmed isolation evidence."""

from __future__ import annotations

import json
import subprocess

import pytest

from opencollab_eval.generation import container_quiescence, gen_prediction_docker
from opencollab_eval.generation.best_of_n_retention import retain_best_of_n_source
from opencollab_eval.generation.gen_prediction_safe_output import persist_generation_failure


@pytest.mark.parametrize("failure", ["initial_inspect", "pause", "pause_inspect", "stop"])
def test_transport_timeout_stops_and_confirms_the_final_state(monkeypatch, failure):
    calls = []
    running = True
    probes = 0

    def docker(command, **kwargs):
        nonlocal running, probes
        operation = command[1]
        calls.append(operation)
        if operation == "inspect":
            probes += 1
            if (failure == "initial_inspect" and probes == 1) or (failure == "pause_inspect" and probes == 2):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, "true false" if running else "false false", "")
        if operation == "pause":
            if failure == "pause":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 1, "", "pause unsupported")
        assert operation == "stop"
        running = False
        if failure == "stop":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "cid", "")

    monkeypatch.setattr(container_quiescence.subprocess, "run", docker)
    assert container_quiescence.isolate_container_for_preservation("cid") == "stopped"
    assert "stop" in calls
    assert calls[-1] == "inspect"
    assert not running


@pytest.mark.parametrize("stop_fails", [False, True])
def test_outer_retention_records_stopped_or_failed_isolation(tmp_path, monkeypatch, stop_fails):
    calls = []
    running = True

    def docker(command, **kwargs):
        nonlocal running
        calls.append(command[1])
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, "true false" if running else "false false", "")
        if command[1] == "pause" or stop_fails:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        assert command[1] == "stop"
        running = False
        return subprocess.CompletedProcess(command, 0, "cid", "")

    def quiesce(_cid):
        raise RuntimeError("solver remains active")

    gen_prediction_docker.write_container_marker(tmp_path, "cid", "candidate")
    monkeypatch.setattr(container_quiescence.subprocess, "run", docker)
    retain_best_of_n_source(
        run_dir=tmp_path, instance={"instance_id": "a__a-1"}, image="fixture", index=0, cid="cid", name="candidate",
        baseline=None, generation_image_id=None, metrics={}, error=RuntimeError("extraction failed"),
        reason="trusted_patch_extraction_incomplete", mark_kept=gen_prediction_docker.mark_container_kept,
        quiesce=quiesce, isolate=container_quiescence.isolate_container_for_preservation,
        persist_failure=persist_generation_failure,
    )
    evidence = json.loads((tmp_path / "generation_failure.json").read_text())["evidence"]
    assert calls == ["inspect", "pause", "stop", "inspect"]
    owner = gen_prediction_docker._read_owner(gen_prediction_docker.container_owner_path(tmp_path, "candidate"))
    assert owner["state"] == "kept"
    assert evidence["container_retained"]
    if stop_fails:
        assert running
        assert "container_isolated" not in evidence
        assert "TimeoutExpired" in evidence["container_isolation_error"]
    else:
        assert not running
        assert evidence["container_isolated"] == "stopped"
