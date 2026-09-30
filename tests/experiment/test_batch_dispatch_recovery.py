"""Run production launch scripts through a local SSH transport stand-in."""

from __future__ import annotations

import dataclasses
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from opencollab_eval.commands.batch import RemoteError, Ssh
from opencollab_eval.experiment import batch_remote
from opencollab_eval.experiment.batch_spec import driver_argv, launch_script, load_host, load_spec
from tests.experiment.batch_support import experiment as experiment
from tests.experiment.batch_support import oc_repo as oc_repo


def _executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o700)


def _fixture(tmp_path, experiment):
    spec = load_spec(experiment["spec"])
    host = dataclasses.replace(
        load_host(experiment["dir"] / "hosts" / "h.yaml"),
        workdir=str(tmp_path), python=str(tmp_path / "driver"),
    )
    counter = tmp_path / "starts"
    prefix = f"#!{sys.executable}\n"
    _executable(Path(host.python), prefix + f"from pathlib import Path\nimport os,time\n"
                f"with Path({str(counter)!r}).open('a') as f: f.write(str(os.getpid())+'\\n')\n"
                "time.sleep(3)\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "setsid", '#!/bin/sh\nexec "$@"\n')
    _executable(bin_dir / "sleep", '#!/bin/sh\n/bin/sleep 0.15\n')
    # The descriptor is inherited from Bash, so its open-file-description
    # lock remains held by Bash after this CLI exits.
    _executable(bin_dir / "flock", prefix + "import fcntl,sys\n"
                "try: fcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                "except BlockingIOError: sys.exit(1)\n")
    _executable(bin_dir / "pgrep", prefix + "import os\nfrom pathlib import Path\n"
                f"path=Path({str(counter)!r})\n"
                "for pid in path.read_text().splitlines() if path.exists() else []:\n"
                " try: os.kill(int(pid),0)\n except ProcessLookupError: continue\n"
                f" print(pid+' '+{shlex.join(driver_argv(spec, host))!r})\n")
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    return spec, host, counter, env


def _cleanup(counter):
    import signal

    for pid in counter.read_text().splitlines() if counter.exists() else []:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def _wait_for_start(counter):
    deadline = time.monotonic() + 5
    while not counter.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert counter.exists()


def test_lost_ack_dispatch_is_sent_once_and_explicit_retry_finds_the_driver(tmp_path, experiment, monkeypatch):
    spec, host, counter, env = _fixture(tmp_path, experiment)
    original_run = subprocess.run
    calls = []

    def ssh(command, **kwargs):
        calls.append(command)
        result = original_run(["/bin/bash", "-s"], input=kwargs["input"], text=True,
                              capture_output=True, env=env, timeout=5)
        assert result.returncode == 0, result.stderr
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 255, "", "lost acknowledgement")
        return result

    monkeypatch.setattr(subprocess, "run", ssh)
    try:
        with pytest.raises(RemoteError, match="255"):
            Ssh(host).run(launch_script(spec, host))
        assert len(calls) == 1
        _wait_for_start(counter)
        assert len(counter.read_text().splitlines()) == 1
        assert Ssh(host).run(launch_script(spec, host)).strip()
        assert len(counter.read_text().splitlines()) == 1
    finally:
        _cleanup(counter)


def test_concurrent_remote_launches_share_the_outdir_lock(tmp_path, experiment):
    spec, host, counter, env = _fixture(tmp_path, experiment)
    script = launch_script(spec, host)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: subprocess.run(
                ["/bin/bash", "-s"], input=script, text=True, capture_output=True, env=env, timeout=5,
            ), range(2)))
        assert all(result.returncode == 0 for result in results)
        assert any(result.stdout.strip() for result in results)
        _wait_for_start(counter)
        assert len(counter.read_text().splitlines()) == 1
    finally:
        _cleanup(counter)


@pytest.mark.parametrize("timeout", [False, True])
def test_only_explicit_read_only_transport_retries(experiment, monkeypatch, timeout):
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    calls = []

    def ssh(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            if timeout:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 255, "", "transport disconnected")
        return subprocess.CompletedProcess(command, 0, "facts\n", "")

    monkeypatch.setattr(subprocess, "run", ssh)
    monkeypatch.setattr("opencollab_eval.commands.batch.time.sleep", lambda _: None)
    assert Ssh(host).run("printf facts", retry_transport=True) == "facts\n"
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(RemoteError):
        Ssh(host).run("launch")
    assert len(calls) == 1


def test_preflight_matches_long_outdir_before_display_truncation(tmp_path, experiment):
    spec = dataclasses.replace(load_spec(experiment["spec"]), name="b" * 180)
    host = load_host(experiment["dir"] / "hosts" / "h.yaml")
    line = "100 " + shlex.join(driver_argv(spec, host))
    _executable(tmp_path / "pgrep", '#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(line) + "\n")
    fragment = next(part for part in batch_remote.preflight_script(spec, host, [], []).splitlines()
                    if part.startswith("pgrep -af") and "RUNNING" in part)
    result = subprocess.run(["/bin/bash", "-c", fragment], capture_output=True, text=True, check=True,
                            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]})
    facts = batch_remote.parse_facts(result.stdout)
    check = next(item for item in batch_remote.evaluate_preflight(spec, host, facts, {}, "")
                 if item.name == "no driver already writing this out-dir")
    assert len(line) > 300
    assert "--out-dir " + spec.name + " " not in line[:300]
    assert not check.ok
    assert len(check.detail) <= 120


def test_slow_launch_with_no_visible_pid_keeps_the_lifecycle_lock(tmp_path, experiment):
    spec, host, counter, env = _fixture(tmp_path, experiment)
    driver = Path(host.python)
    source = driver.read_text().replace(
        "from pathlib import Path", "import time\ntime.sleep(0.5)\nfrom pathlib import Path",
    )
    driver.write_text(source)
    _executable(tmp_path / "bin" / "pgrep", "#!/bin/sh\nexit 0\n")
    try:
        for _ in range(2):
            result = subprocess.run(["/bin/bash", "-s"], input=launch_script(spec, host), text=True,
                                    capture_output=True, env=env, timeout=5)
            assert result.returncode == 0, result.stderr
        assert "launch already active" in result.stdout
        _wait_for_start(counter)
        assert len(counter.read_text().splitlines()) == 1
    finally:
        _cleanup(counter)
