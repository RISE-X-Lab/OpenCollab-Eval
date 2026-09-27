import json
import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_docker as docker
from opencollab_eval.generation.container_resource_evidence import collect_resource_evidence


def result(value, returncode=0):
    return subprocess.CompletedProcess([], returncode, json.dumps(value), "")


@pytest.mark.parametrize("value", [{}, [], [None], [{"State": []}]])
def test_unavailable_evidence_keeps_unknown_fields(value):
    evidence = collect_resource_evidence("container", lambda: result(value))
    assert evidence["capture_status"] == "unavailable"
    assert evidence["state"]["oom_killed"] is None
    assert evidence["host_config"]["memory_bytes"] is None
    assert evidence["errors"]


def test_cgroup_kill_is_retained_when_container_itself_survived(monkeypatch):
    files = {
        "/proc/42/cgroup": "0::/test-cgroup\n",
        "/sys/fs/cgroup/test-cgroup/memory.events": "oom 1\noom_kill 1\n",
        "/sys/fs/cgroup/test-cgroup/memory.peak": "4294967296\n",
    }
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: files[str(path)])
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in files)
    evidence = collect_resource_evidence("container", lambda: result([{
        "Id": "a" * 64,
        "State": {"Pid": 42, "OOMKilled": False, "ExitCode": 0, "Status": "running"},
        "HostConfig": {"Memory": 4294967296, "MemorySwap": 8589934592},
    }]))
    assert evidence["state"]["oom_killed"] is False
    assert evidence["cgroup"]["memory_events"]["oom_kill"] == 1
    assert evidence["cgroup"]["memory_peak_bytes"] == 4294967296
    assert evidence["errors"] == []


def test_capture_precedes_removal_and_failure_does_not_prevent_cleanup(monkeypatch, tmp_path):
    path = tmp_path / "evidence.json"
    commands = []

    def invoke(*args, **kwargs):
        commands.append(args)
        if args[0] == "inspect":
            return subprocess.CompletedProcess([], 1, "", "inspect unavailable")
        assert path.is_file()
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(docker, "_docker", invoke)
    monkeypatch.setattr(docker, "_container_is_absent", lambda reference: True)
    assert docker.remove_container("owned", evidence_path=path)
    evidence = json.loads(path.read_text())["captures"][0]["evidence"]
    assert commands[0][0] == "inspect"
    assert commands[1][0] == "rm"
    assert evidence["state"]["oom_killed"] is None
    assert evidence["errors"]


def test_foreign_owner_is_not_inspected_or_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(docker, "_container_owner_label_state", lambda *a: "foreign")
    monkeypatch.setattr(docker, "remove_container", lambda *a, **k: pytest.fail("foreign removal"))
    assert not docker._remove_labeled_container(
        "foreign", "token", foreign_proves_absence=False, evidence_path=tmp_path / "evidence.json"
    )
