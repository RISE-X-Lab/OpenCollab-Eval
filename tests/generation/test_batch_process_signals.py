"""Exercise real batch processes, signal delivery, and scoped ownership recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from opencollab_eval.generation import batch_processes
from opencollab_eval.generation import gen_prediction_docker as owners


def _wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("fixture did not reach the expected state")


def _fixture(tmp_path):
    generator = tmp_path / "generator.py"
    generator.write_text(
        "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "iid=Path(sys.argv[sys.argv.index('--instance-file')+1]).stem\n"
        f"root=Path({str(tmp_path)!r})\n"
        "def stop(*_):\n (root/(iid+'.stopped')).write_text('stopped')\n raise SystemExit(130)\n"
        "signal.signal(signal.SIGINT,stop)\n"
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; '"
        "'signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)'])\n"
        "(root/(iid+'.child')).write_text(str(child.pid))\n"
        "(root/(iid+'.started')).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\nfrom opencollab_eval.generation import gen_prediction_batch as batch\n"
        f"generator={str(generator)!r}\n"
        "def command(**kwargs):\n"
        " return [sys.executable,generator,'--instance-file',str(kwargs['instance_path']),"
        "'--output',str(kwargs['predictions'])]\n"
        "batch.build_command=command\nraise SystemExit(batch.main(sys.argv[1:]))\n"
    )
    instances = tmp_path / "instances.jsonl"
    instances.write_text("".join(json.dumps({"instance_id": f"a__a-{i}", "repo": "a/a"}) + "\n" for i in range(6)))
    return [sys.executable, str(runner), "--instances", str(instances), "--arm", "single",
            "--out-dir", str(tmp_path / "out"), "--concurrency", "2"]


def _alive(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_stop_launches_no_queued_tasks_and_stops_owned_processes(tmp_path, signum):
    command = _fixture(tmp_path)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    with (tmp_path / "outer.log").open("w") as log:
        driver = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            _wait_for(lambda: len(list(tmp_path.glob("*.started"))) == 2)
            os.kill(driver.pid, signum)
            assert driver.wait(timeout=8) == 128 + signum
            assert len(list(tmp_path.glob("*.started"))) == 2
            assert len(list(tmp_path.glob("*.stopped"))) == 2
            assert unrelated.poll() is None
            for path in tmp_path.glob("*.child"):
                _wait_for(lambda path=path: not _alive(int(path.read_text())))
            manifest = [json.loads(line) for line in (tmp_path / "out" / "manifest.jsonl").read_text().splitlines()]
            assert len(manifest) == 2
            assert {row["instance_id"] for row in manifest} == {"a__a-0", "a__a-1"}
        finally:
            if driver.poll() is None:
                os.kill(driver.pid, signal.SIGTERM)
                driver.wait(timeout=8)
            unrelated.terminate()
            unrelated.wait(timeout=5)


def test_two_real_batch_entries_dispatch_the_models_once(tmp_path):
    command = _fixture(tmp_path)
    with (tmp_path / "outer.log").open("w") as log:
        first = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            _wait_for(lambda: len(list(tmp_path.glob("*.started"))) == 2)
            second = subprocess.run(command, capture_output=True, text=True, timeout=5)
            assert second.returncode == 0
            assert "already running" in second.stdout
            assert len(list(tmp_path.glob("*.started"))) == 2
        finally:
            os.kill(first.pid, signal.SIGTERM)
            assert first.wait(timeout=8) == 143


def test_interrupted_owner_recovery_preserves_candidates_and_ignores_other_owners(tmp_path, monkeypatch):
    command = ["generator", "--output", str(tmp_path / "preds.jsonl"), "--instance-file", "a__a-1.json"]
    records = []
    for name, pid, identity, state in (
        ("active", 999999, "owned", "active"), ("kept", 999999, "owned", "kept"),
        ("foreign", 999998, "other", "active"), ("reused", 999999, "older", "active"),
    ):
        run_dir = tmp_path / name
        owners.write_container_marker(run_dir, name + "-cid", name)
        path = owners.container_owner_path(run_dir, name)
        original = owners._read_owner(path)
        owners._replace_owner(
            path, original, {**original, "owner_pid": pid, "owner_start_identity": identity, "state": state},
        )
        records.append(path)
    removed, isolated = [], []
    monkeypatch.setattr(owners, "_owner_is_live", lambda record: False)
    monkeypatch.setattr(
        owners, "_remove_owned_container", lambda record: removed.append(record["container_name"]) or True,
    )
    monkeypatch.setattr(owners, "_container_owner_label_state", lambda *args: "matching")
    monkeypatch.setattr("opencollab_eval.generation.container_quiescence.isolate_container_for_preservation",
                        lambda cid: isolated.append(cid) or "stopped")
    batch_processes._recover_generator_containers(command, 999999, "owned")
    assert removed == ["active"]
    assert isolated == ["kept-cid"]
    assert not records[0].exists()
    assert all(path.exists() for path in records[1:])
    evidence = json.loads((tmp_path / "kept" / "generation_failure.json").read_text())["evidence"]
    assert evidence["container_isolated"] == "stopped"
