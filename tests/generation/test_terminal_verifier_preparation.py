"""Task preparation preserves candidate bytes and executes real shell probes."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import shutil
import tarfile
from pathlib import Path

import pytest

from opencollab_eval.generation import terminal_verifier_preparation as preparation
from opencollab_eval.generation.terminal_container_backend import CommandResult
from opencollab_eval.generation.terminal_verifier import run_terminal_verifier


class ShellEnvironment:
    """Run production Bash in a temporary filesystem without a Docker daemon."""

    container = "isolated-verifier-clone"

    def __init__(self, root):
        self.root = root
        (root / "tmp").mkdir()
        (root / "app").mkdir()
        self.frame = root / "tmp/frame.bmp"
        self.commands = []
        self.quiescence_calls = 0
        self.quiescence_error = None
        self.before_move = None
        self.after_move = None

    async def ensure_quiescent(self):
        self.quiescence_calls += 1
        if self.quiescence_error:
            raise RuntimeError(self.quiescence_error)

    async def exec_cmd(self, cmd, timeout=120, stdin=None):
        self.commands.append((cmd, stdin, timeout))
        command = cmd.replace("/tmp/", str(self.root / "tmp") + "/")
        command = command.replace("/app/", str(self.root / "app") + "/")
        if shutil.which("gstat"):
            command = command.replace("stat --printf", "gstat --printf")
        if shutil.which("gmv"):
            command = command.replace("mv -", "gmv -")
        if command.startswith("\ndirectory="):
            if self.before_move:
                self.before_move()
            if self.after_move:
                command = command.replace('if [ -L "$held" ]', self.after_move + '\nif [ -L "$held" ]', 1)
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return CommandResult(process.returncode, stdout.decode(), stderr.decode())

    def translated_path(self, path):
        return Path(path.replace("/tmp/", str(self.root / "tmp") + "/"))


def archive_bytes(content=b"old-frame\x00\xff", *, name="frame.bmp", kind=tarfile.REGTYPE, mode=0o640):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.mode = mode
        member.type = kind
        member.linkname = "../../outside" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
        member.size = len(content) if member.isfile() else 0
        archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return output.getvalue()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    (tmp_path / "clone").mkdir()
    env = ShellEnvironment(tmp_path / "clone")

    def docker(*args, timeout):
        assert env.quiescence_calls == 1
        assert args == ("cp", env.container + ":/tmp/frame.bmp", "-")
        assert timeout > 0
        return archive_bytes(env.frame.read_bytes(), mode=env.frame.stat().st_mode & 0o7777)

    monkeypatch.setattr(preparation.backend, "docker", docker)
    return env


@pytest.mark.parametrize("task_name", sorted(preparation.MIPS_TASKS))
async def test_old_frame_is_saved_then_moved_without_changing_candidate(environment, tmp_path, task_name):
    environment.frame.write_bytes(b"old-frame\x00\xff")
    environment.frame.chmod(0o640)
    candidate = environment.root / "app/program.bin"
    candidate.write_bytes(b"candidate\x00\xff")
    other_tmp = environment.root / "tmp/keep.txt"
    other_tmp.write_bytes(b"keep")

    receipt = await preparation.prepare_terminal_verifier(task_name, environment, tmp_path / "artifacts")

    assert receipt["status"] == "ready"
    assert environment.quiescence_calls == 1
    assert not environment.frame.exists()
    assert Path(receipt["content_path"]).read_bytes() == b"old-frame\x00\xff"
    assert Path(receipt["archive_path"]).read_bytes() == archive_bytes()
    evidence = json.loads(Path(receipt["archive_path"]).with_name("frame.json").read_text())
    assert evidence["type"] == "file" and evidence["mode"] == 0o640
    held = environment.translated_path(receipt["quarantine_path"])
    assert held.read_bytes() == b"old-frame\x00\xff"
    assert held.stat().st_mode & 0o7777 == 0o640
    assert candidate.read_bytes() == b"candidate\x00\xff"
    assert other_tmp.read_bytes() == b"keep"


@pytest.mark.parametrize("task_name", sorted(preparation.MIPS_TASKS))
async def test_missing_frame_is_ready_without_an_archive_or_mutation(environment, tmp_path, monkeypatch, task_name):
    monkeypatch.setattr(preparation.backend, "docker", lambda *args, **kwargs: pytest.fail("unneeded archive"))
    receipt = await preparation.prepare_terminal_verifier(task_name, environment, tmp_path / "artifacts")
    assert receipt["action"] == "frame_absent"
    assert environment.quiescence_calls == 1
    assert not (tmp_path / "artifacts").exists()
    assert list((environment.root / "tmp").iterdir()) == []


@pytest.mark.parametrize("failure", ["write", "readback"])
async def test_archive_io_failure_keeps_the_original(environment, tmp_path, monkeypatch, failure):
    environment.frame.write_bytes(b"original")
    real_open, real_read = Path.open, Path.read_bytes

    def fail_open(path, *args, **kwargs):
        if path.name == "frame.bmp.tar":
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    def corrupt_read(path):
        return b"corrupt" if path.name == "frame.bmp.tar" else real_read(path)

    monkeypatch.setattr(
        Path, "open" if failure == "write" else "read_bytes", fail_open if failure == "write" else corrupt_read
    )
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    assert caught.value.reason == "frame_backup_failed"
    assert environment.frame.read_bytes() == b"original"
    assert len(environment.commands) == 1


@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "directory", "fifo"])
async def test_non_regular_frame_is_retained(environment, tmp_path, monkeypatch, kind):
    target = environment.root / "tmp/target"
    target.write_bytes(b"target")
    if kind in {"symlink", "dangling_symlink"}:
        environment.frame.symlink_to(target if kind == "symlink" else target.with_name("missing"))
    elif kind == "directory":
        environment.frame.mkdir()
    else:
        os.mkfifo(environment.frame)
    monkeypatch.setattr(preparation.backend, "docker", lambda *args, **kwargs: pytest.fail("unneeded archive"))
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-mips-interpreter", environment, tmp_path / "artifacts")
    assert caught.value.reason == "unsupported_frame_type"
    assert os.path.lexists(environment.frame)
    assert target.read_bytes() == b"target"
    assert len(environment.commands) == 1


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("../../outside", tarfile.REGTYPE),
        ("/frame.bmp", tarfile.REGTYPE),
        ("tmp/frame.bmp", tarfile.REGTYPE),
        ("frame.bmp/child", tarfile.REGTYPE),
        ("frame.bmp", tarfile.SYMTYPE),
        ("frame.bmp", tarfile.LNKTYPE),
        ("frame.bmp", tarfile.DIRTYPE),
    ],
)
async def test_archive_paths_and_links_are_rejected_without_extraction(environment, tmp_path, monkeypatch, name, kind):
    environment.frame.write_bytes(b"old-frame\x00\xff")
    monkeypatch.setattr(preparation.backend, "docker", lambda *args, **kwargs: archive_bytes(name=name, kind=kind))
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    assert caught.value.reason == "unsafe_frame_archive"
    assert environment.frame.read_bytes() == b"old-frame\x00\xff"
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "outside").exists()


async def test_retry_uses_a_new_backup_and_preserves_the_first(environment, tmp_path):
    environment.frame.write_bytes(b"first")
    first = await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    environment.frame.write_bytes(b"second")
    environment.quiescence_calls = 0
    second = await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    assert first["archive_path"] != second["archive_path"]
    assert Path(first["content_path"]).read_bytes() == b"first"
    assert Path(second["content_path"]).read_bytes() == b"second"


@pytest.mark.parametrize("when", ["before_move", "after_move"])
async def test_changed_frame_is_restored_and_never_deleted(environment, tmp_path, when):
    environment.frame.write_bytes(b"old")
    if when == "before_move":
        environment.before_move = lambda: environment.frame.write_bytes(b"changed")
    else:
        environment.after_move = 'printf changed > "$held"'
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    assert caught.value.reason == "frame_changed"
    assert environment.frame.read_bytes() == b"changed"
    assert Path(caught.value.receipt["content_path"]).read_bytes() == b"old"


async def test_concurrent_replacement_preserves_both_files(environment, tmp_path):
    environment.frame.write_bytes(b"old")
    environment.after_move = f"printf replacement > {shlex.quote(str(environment.frame))}"
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    receipt = caught.value.receipt
    assert caught.value.reason == "frame_changed"
    assert receipt["retained_at"] == receipt["quarantine_path"]
    assert environment.frame.read_bytes() == b"replacement"
    assert environment.translated_path(receipt["quarantine_path"]).read_bytes() == b"old"


@pytest.mark.parametrize("probe", [None, "", "exit 17"])
async def test_login_failure_stops_before_verifier(environment, tmp_path, probe):
    verifier_ran = False
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier(
            "configure-git-webserver", environment, tmp_path / "artifacts", login_probe=probe
        )
        verifier_ran = True
    assert caught.value.reason == ("login_probe_missing" if not probe else "login_probe_failed")
    assert not verifier_ran
    assert environment.quiescence_calls == 1


async def test_login_executes_probe_without_recording_its_text_or_output(environment, tmp_path):
    marker = tmp_path / "login-executed"
    secret = "sensitive-login-value"
    probe = f"printf '%s' {shlex.quote(secret)}; printf ran > {shlex.quote(str(marker))}"
    receipt = await preparation.prepare_terminal_verifier(
        "configure-git-webserver", environment, tmp_path / "artifacts", login_probe=probe
    )
    assert marker.read_text() == "ran"
    assert receipt["login_probe_exit_code"] == 0
    assert secret not in json.dumps(receipt)
    assert secret not in environment.commands[0][0]
    assert environment.commands[0][1] == probe.encode()
    assert not (tmp_path / "artifacts").exists()


async def test_unrelated_task_runs_without_preparation(environment, tmp_path):
    environment.frame.write_bytes(b"unrelated")
    receipt = await preparation.prepare_terminal_verifier("other-task", environment, tmp_path / "artifacts")
    assert receipt["action"] == "none"
    assert environment.commands == []
    assert environment.quiescence_calls == 0
    assert environment.frame.read_bytes() == b"unrelated"


async def test_non_quiescent_clone_keeps_original_and_runs_no_commands(environment, tmp_path):
    environment.frame.write_bytes(b"original")
    environment.quiescence_error = "active commands"
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    assert caught.value.reason == "workspace_not_quiescent"
    assert environment.frame.read_bytes() == b"original"
    assert environment.commands == []


async def test_preparation_is_bounded_and_external_cancellation_propagates(environment, tmp_path, monkeypatch):
    async def blocked():
        await asyncio.Event().wait()

    monkeypatch.setattr(environment, "ensure_quiescent", blocked)
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier(
            "make-doom-for-mips", environment, tmp_path / "artifacts", timeout=0.02
        )
    assert caught.value.reason == "preparation_timeout"
    task = asyncio.create_task(
        preparation.prepare_terminal_verifier("make-doom-for-mips", environment, tmp_path / "artifacts")
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_genuine_candidate_error_still_fails_executed_verifier(environment, tmp_path):
    environment.frame.write_bytes(b"stale")
    program = environment.root / "app/program.sh"
    candidate = f"#!/bin/bash\nprintf incorrect > {shlex.quote(str(environment.frame))}\n".encode()
    program.write_bytes(candidate)
    reference = environment.root / "app/reference.bmp"
    reference.write_bytes(b"correct")
    await preparation.prepare_terminal_verifier("make-mips-interpreter", environment, tmp_path / "artifacts")
    result = await environment.exec_cmd("/bin/bash /app/program.sh && cmp -s /tmp/frame.bmp /app/reference.bmp")
    assert result.returncode != 0
    assert environment.frame.read_bytes() == b"incorrect"
    assert program.read_bytes() == candidate


async def test_failed_login_pipeline_cannot_be_hidden_by_a_later_success(environment, tmp_path):
    marker = tmp_path / "should-not-run"
    probe = f"false | cat\nprintf done > {shlex.quote(str(marker))}"
    with pytest.raises(preparation.VerifierPreparationError) as caught:
        await preparation.prepare_terminal_verifier(
            "configure-git-webserver", environment, tmp_path / "artifacts", login_probe=probe,
        )
    assert caught.value.reason == "login_probe_failed"
    assert not marker.exists()


@pytest.mark.parametrize("external_cancel", [False, True])
async def test_scoring_cancellation_after_frame_move_retains_recovery_evidence(
    environment, tmp_path, monkeypatch, external_cancel,
):
    environment.frame.write_bytes(b"old-frame")
    entered = asyncio.Event()
    aborted = False
    original_exec = environment.exec_cmd

    async def delayed_response(command, timeout=120, stdin=None):
        result = await original_exec(command, timeout=timeout, stdin=stdin)
        if command.startswith("\ndirectory="):
            entered.set()
            await asyncio.Event().wait()
        return result

    async def abort():
        nonlocal aborted
        aborted = True

    async def verifier(remaining):
        pytest.fail("frame preparation has not completed")

    monkeypatch.setattr(environment, "exec_cmd", delayed_response)
    monkeypatch.setattr(environment, "abort", abort, raising=False)
    monkeypatch.setattr(
        preparation.backend, "docker",
        lambda *args, **kwargs: archive_bytes(
            environment.frame.read_bytes(), mode=environment.frame.stat().st_mode & 0o7777,
        ),
    )
    receipt = {}
    task = asyncio.create_task(run_terminal_verifier(
        "make-doom-for-mips", environment, tmp_path / "artifacts", verifier,
        timeout_seconds=5 if external_cancel else 0.2,
        preparation_receipt=receipt,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    if external_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["status"] == "facility_error"
        assert result["timed_out"] is True
        assert result["reward"] is None
        assert result["preparation"] is receipt
    assert aborted
    assert receipt["status"] == "error"
    assert not environment.frame.exists()
    assert environment.translated_path(receipt["quarantine_path"]).read_bytes() == b"old-frame"
    assert Path(receipt["content_path"]).read_bytes() == b"old-frame"
    assert Path(receipt["archive_path"]).is_file()
