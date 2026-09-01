from __future__ import annotations

from swe_eval_status_support import (
    Path,
    _patch,
    _per_instance_run_kwargs,
    _spawn_normal_exit_with_term_ignoring_descendant,
    _spawn_term_ignoring_descendant,
    importlib,
    io,
    json,
    os,
    pytest,
    signal,
    subprocess,
    sys,
    threading,
    time,
)


def test_per_instance_decodes_wait_status_without_python39_helper(monkeypatch):
    importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    process = sys.modules["opencollab_eval.commands.swebench_eval_process"]
    monkeypatch.setattr(process.os, "waitstatus_to_exitcode", None)

    assert process._decode_wait_status(7 << 8) == 7
    assert process._decode_wait_status(signal.SIGTERM) == -signal.SIGTERM


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="identity claims use POSIX process groups")
def test_residual_group_with_unavailable_identity_probe_is_retained(monkeypatch):
    process = importlib.import_module("opencollab_eval.commands.swebench_eval_process")
    monkeypatch.setattr(process, "_runner", lambda: process)
    monkeypatch.setattr(process, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(process, "process_start_identity", lambda _pgid: "")

    assert process._claim_residual_group_is_live(
        {
            "evaluator_pgid": 424242,
            "evaluator_start_identity": "proc:expected",
        }
    ) is True


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="identity claims use POSIX process groups")
def test_residual_group_with_identity_mismatch_is_reclaimable(monkeypatch):
    process = importlib.import_module("opencollab_eval.commands.swebench_eval_process")
    monkeypatch.setattr(process, "_runner", lambda: process)
    monkeypatch.setattr(process, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(process, "process_start_identity", lambda _pgid: "proc:actual")

    assert process._claim_residual_group_is_live(
        {
            "evaluator_pgid": 424242,
            "evaluator_start_identity": "proc:expected",
        }
    ) is False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="identity claims use POSIX process groups")
def test_residual_group_absence_is_reclaimable(monkeypatch):
    process = importlib.import_module("opencollab_eval.commands.swebench_eval_process")
    monkeypatch.setattr(process, "_runner", lambda: process)
    monkeypatch.setattr(process, "_process_group_exists", lambda _pgid: False)

    assert process._claim_residual_group_is_live(
        {
            "evaluator_pgid": 424242,
            "evaluator_start_identity": "proc:expected",
        }
    ) is False


@pytest.mark.skipif(os.name != "posix", reason="owned evaluator uses POSIX process groups")
def test_per_instance_owned_evaluator_uses_popen_without_python_fork(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    calls = []

    class FakeProcess:
        pid = 424240
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM

        def kill(self):
            self.returncode = -signal.SIGKILL

    def fake_popen(command, **popen_kwargs):
        calls.append((command, popen_kwargs))
        return FakeProcess()

    monkeypatch.setattr(runner, "_EVALUATOR_POPEN", fake_popen)
    monkeypatch.setattr(
        runner.os,
        "fork",
        lambda: pytest.fail("owned evaluator must not call Python os.fork"),
    )
    log = tmp_path / "eval.log"
    with log.open("a", encoding="utf-8") as handle:
        process = runner._spawn_owned_evaluator(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env=os.environ.copy(),
            log_fd=handle.fileno(),
            wall_timeout=0.2,
            spawn_timeout=1.0,
        )

    assert isinstance(process, runner.OwnedEvaluatorProcess)
    assert len(calls) == 1
    assert calls[0][1]["start_new_session"] is True
    assert process.wait(timeout=0.2) == 0


@pytest.mark.skipif(os.name != "posix", reason="owned evaluator uses POSIX process groups")
def test_per_instance_owned_evaluator_preserves_spawn_timeout_bound(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    process_mod = importlib.import_module("opencollab_eval.commands.swebench_eval_process")

    class FakeProcess:
        pid = 424241
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM

        def kill(self):
            self.returncode = -signal.SIGKILL

    monkeypatch.setattr(runner, "_EVALUATOR_POPEN", lambda *args, **kwargs: FakeProcess())
    clock = iter((1.0, 1.1, 1.1))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    cleanup_calls = []

    def fake_cleanup(process, *, term_timeout, kill_timeout):
        cleanup_calls.append((process.pid, term_timeout, kill_timeout))
        return True, []

    monkeypatch.setattr(process_mod, "_terminate_process_group_owned", fake_cleanup)
    log = tmp_path / "eval.log"
    with log.open("a", encoding="utf-8") as handle:
        with pytest.raises(runner.EvaluatorSpawnTimeout):
            runner._spawn_owned_evaluator(
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                env=os.environ.copy(),
                log_fd=handle.fileno(),
                wall_timeout=0.2,
                spawn_timeout=0.05,
            )

    assert cleanup_calls == [(424241, 0.05, runner.PROCESS_KILL_REAP_TIMEOUT_SECONDS)]


@pytest.mark.skipif(os.name != "posix", reason="identity probe uses POSIX subprocess")
def test_per_instance_identity_probe_popen_timeout_is_reaped(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    calls = []

    class FakeProbe:
        returncode = None

        def communicate(self, timeout=None):
            calls.append(timeout)
            raise subprocess.TimeoutExpired("ps", timeout)

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            assert timeout is not None
            self.returncode = -signal.SIGKILL
            return self.returncode

        def poll(self):
            return self.returncode

    probe = FakeProbe()

    def fake_popen(*args, **kwargs):
        del args, kwargs
        return probe

    original_is_dir = runner.Path.is_dir

    def no_proc(path):
        return False if str(path) == "/proc" else original_is_dir(path)

    monkeypatch.setattr(runner.Path, "is_dir", no_proc)
    monkeypatch.setattr(runner, "_PROCESS_IDENTITY_POPEN", fake_popen)
    monkeypatch.setattr(runner, "PROCESS_IDENTITY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runner, "PROCESS_KILL_REAP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        runner.os,
        "fork",
        lambda: pytest.fail("identity probe must not call Python os.fork"),
    )

    assert runner.process_start_identity(os.getpid()) == ""
    assert calls == [0.05]
    assert probe.returncode == -signal.SIGKILL


@pytest.mark.skipif(os.name != "posix", reason="identity probe uses POSIX subprocess")
def test_per_instance_identity_probe_preserves_legacy_ps_value(
    monkeypatch,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")

    class FakeProbe:
        returncode = 0

        def communicate(self, timeout=None):
            assert timeout == runner.PROCESS_IDENTITY_TIMEOUT_SECONDS
            return "Mon Jan  1 00:00:00 2024\n", ""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(runner.Path, "is_dir", lambda _path: False)
    monkeypatch.setattr(runner, "_PROCESS_IDENTITY_POPEN", lambda *args, **kwargs: FakeProbe())

    assert runner.process_start_identity(os.getpid()) == "Mon Jan  1 00:00:00 2024"


@pytest.mark.skipif(os.name != "posix", reason="identity probe uses POSIX subprocess")
def test_per_instance_identity_probe_falls_back_when_proc_abi_missing(
    monkeypatch,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")

    class FakeProbe:
        returncode = 0

        def communicate(self, timeout=None):
            return "Tue Jan  2 00:00:00 2024\n", ""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(runner.Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(runner, "_proc_process_start_identity", lambda _pid: "")
    monkeypatch.setattr(runner, "_PROCESS_IDENTITY_POPEN", lambda *args, **kwargs: FakeProbe())

    assert runner.process_start_identity(os.getpid()) == "Tue Jan  2 00:00:00 2024"


def test_per_instance_identity_probe_rejects_malformed_pid_without_spawning(monkeypatch):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    calls = []
    monkeypatch.setattr(runner, "_PROCESS_IDENTITY_POPEN", lambda *args, **kwargs: calls.append(args))

    assert runner.process_start_identity("not-a-pid") == ""
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX sessions")
def test_per_instance_helper_normal_exit_cleans_lingering_descendant(tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    started = tmp_path / "owned.started"
    finished = tmp_path / "owned.finished"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(started)!r}).touch();"
        "time.sleep(0.8);"
        f"pathlib.Path({str(finished)!r}).touch()"
    )
    parent_code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_code!r}]);time.sleep(0.1)"
    log = tmp_path / "eval.log"
    with log.open("a", encoding="utf-8") as handle:
        process = runner._spawn_owned_evaluator(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env=os.environ.copy(),
            log_fd=handle.fileno(),
            wall_timeout=1.0,
            spawn_timeout=0.2,
        )
        assert process.wait(timeout=1.0) == 0
        assert runner.ensure_process_group_quiesced_after_wait(process, handle) is True

    assert started.exists()
    time.sleep(0.9)
    assert not finished.exists()


@pytest.mark.skipif(os.name != "posix", reason="recoverable helper uses POSIX wall bound")
def test_per_instance_helper_wait_uses_total_wall_deadline(tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    log = tmp_path / "eval.log"
    started = time.monotonic()
    with log.open("a", encoding="utf-8") as handle:
        process = runner._spawn_owned_evaluator(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            log_fd=handle.fileno(),
            wall_timeout=0.15,
            # Keep enough launch slack for a loaded CI/macOS host; the
            # assertion below targets the total wall deadline, not spawn.
            spawn_timeout=1.0,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        assert (
            runner.terminate_process_group(
                process,
                handle,
                term_timeout=0.05,
                kill_timeout=0.2,
            )
            is True
        )

    assert time.monotonic() - started < 1.0


def test_per_instance_uses_immutable_candidate_and_requires_report(monkeypatch, tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    commands = []

    class FakeProcess:
        pid = 999_999_902

        def wait(self, timeout=None):
            report = runner.report_path(kwargs["work_dir"], "run", "model", "task-1")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"task-1": {"resolved": True}}),
                encoding="utf-8",
            )
            return 0

    def fake_popen(command, **popen_kwargs):
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    result = runner.run_one(**kwargs)
    candidate_path = Path(commands[0][commands[0].index("-p") + 1])

    assert result == ("task-1", 0)
    assert candidate_path != tmp_path / "predictions.jsonl"
    assert runner.read_jsonl(candidate_path) == [prediction]
    attempt = json.loads(
        runner.identity_path(runner.report_path(kwargs["work_dir"], "run", "model", "task-1")).read_text(
            encoding="utf-8"
        )
    )
    assert attempt["status"] == "completed"
    assert attempt["pid"] == 0


def test_per_instance_claim_blocks_concurrent_duplicate(monkeypatch, tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    entered = threading.Event()
    release = threading.Event()
    popen_count = 0

    class FakeProcess:
        pid = 999_999_903

        def wait(self, timeout=None):
            entered.set()
            release.wait(timeout=2)
            report = runner.report_path(kwargs["work_dir"], "run", "model", "task-1")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"task-1": {"resolved": False}}),
                encoding="utf-8",
            )
            return 0

    def fake_popen(command, **popen_kwargs):
        nonlocal popen_count
        popen_count += 1
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    results = []
    first = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    second = threading.Thread(target=lambda: results.append(runner.run_one(**kwargs)))
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    release.set()
    first.join(timeout=2)

    assert popen_count == 1
    assert sorted(results) == [("task-1", 0), ("task-1", 0)]


def test_per_instance_zero_exit_without_report_is_failure(monkeypatch, tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)

    class FakeProcess:
        pid = 999_999_904

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    assert runner.run_one(**kwargs) == ("task-1", 3)


def test_per_instance_started_identity_failure_terminates_child(monkeypatch, tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    original_write_identity = runner.write_identity
    identity_writes = 0
    signals = []

    def flaky_write_identity(*args, **write_kwargs):
        nonlocal identity_writes
        identity_writes += 1
        if identity_writes >= 2:
            raise OSError("disk unavailable")
        return original_write_identity(*args, **write_kwargs)

    class FakeProcess:
        pid = 424242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner, "write_identity", flaky_write_identity)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = runner.run_one(**kwargs)

    assert result == ("task-1", 4)
    assert signals == [(424242, signal.SIGTERM)]
    assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()


def test_per_instance_kill_reap_timeout_is_bounded_and_consumed(monkeypatch):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    release = threading.Event()
    consumer_started = threading.Event()
    consumer_finished = threading.Event()
    waits = []
    signals = []

    class StubbornProcess:
        pid = 424243

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            consumer_started.set()
            release.wait(timeout=1)
            consumer_finished.set()
            return 0

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    log = io.StringIO()

    reaped = runner.terminate_process_group(
        StubbornProcess(),
        log,
        term_timeout=0.001,
        kill_timeout=0.001,
    )

    assert reaped is False
    assert signals == [
        (424243, signal.SIGTERM),
        (424243, signal.SIGKILL),
    ]
    assert len(waits) >= 2
    assert all(0 <= timeout <= 0.001 for timeout in waits[:2])
    assert consumer_started.wait(timeout=0.2)
    assert "technical cleanup failure" in log.getvalue()
    release.set()
    assert consumer_finished.wait(timeout=0.2)


def test_per_instance_cleanup_kills_descendant_after_leader_exits(tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    process = _spawn_term_ignoring_descendant(tmp_path)
    try:
        quiesced = runner.terminate_process_group(
            process,
            io.StringIO(),
            term_timeout=0.05,
            kill_timeout=1.0,
        )

        assert quiesced is True
        assert process.poll() is not None
        assert runner._process_group_exists(process.pid) is False
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def test_per_instance_normal_exit_cleans_residual_descendants(monkeypatch, tmp_path):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    process = _spawn_normal_exit_with_term_ignoring_descendant(tmp_path)
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)
    real_terminate = runner.terminate_process_group

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        runner,
        "terminate_process_group",
        lambda child, log: real_terminate(
            child,
            log,
            term_timeout=0.05,
            kill_timeout=1.0,
        ),
    )
    try:
        result = runner.run_one(**kwargs)

        assert result == ("task-1", 3)
        assert runner._process_group_exists(process.pid) is False
        assert not runner._claim_path(kwargs["work_dir"], "task-1").exists()
        log = kwargs["work_dir"] / "command_logs" / "task-1.log"
        assert "residual process group" in log.read_text(encoding="utf-8")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def test_per_instance_normal_exit_cleanup_failure_retains_claim(
    monkeypatch,
    tmp_path,
):
    runner = importlib.import_module("opencollab_eval.commands.run_swebench_eval_per_instance")
    prediction = {
        "instance_id": "task-1",
        "record_id": "r1",
        "model_name_or_path": "model",
        "model_patch": _patch("+current\n"),
    }
    kwargs = _per_instance_run_kwargs(runner, tmp_path, prediction)

    class ReapedLeader:
        pid = 424261

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: ReapedLeader(),
    )
    monkeypatch.setattr(runner, "_process_group_exists", lambda pgid: True)
    monkeypatch.setattr(runner, "terminate_process_group", lambda *args: False)
    monkeypatch.setattr(runner, "process_start_identity", lambda pid: f"start-{pid}")

    result = runner.run_one(**kwargs)
    claim = json.loads(runner._claim_path(kwargs["work_dir"], "task-1").read_text(encoding="utf-8"))

    assert result == ("task-1", runner.PROCESS_CLEANUP_FAILED_EXIT_CODE)
    assert claim["status"] == "cleanup_failed"
    assert claim["evaluator_pgid"] == 424261
