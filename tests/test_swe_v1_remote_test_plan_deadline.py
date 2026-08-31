from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from opencollab_eval.engine import swe_v1_remote_test_plan as plans


def test_controller_timeout_rejects_boolean() -> None:
    plan = {"commands": ["printf ok"], "proofs": [{}]}
    try:
        plans.prolite_test_plan_script(plan, "f2p", "nonce", controller_timeout=True)
    except ValueError as exc:
        assert "finite and positive" in str(exc)
    else:
        raise AssertionError("boolean timeout must be rejected")


def test_multi_batch_controller_uses_one_end_to_end_deadline(monkeypatch) -> None:
    monkeypatch.setattr(plans, "validated_test_plan_kind", lambda *args, **kwargs: "pytest")
    script = plans.prolite_test_plan_script(
        {
            "commands": ["pytest first.py", "pytest second.py"],
            "proofs": [
                {"kind": "pytest_structured_reports"},
                {"kind": "pytest_structured_reports"},
            ],
        },
        "f2p",
        "nonce",
        controller_timeout=7,
    )

    assert script.count("controller_deadline=") == 1
    assert script.count("export batch_timeout") == 2
    # Both the controller's event timeout and the outer process watchdog are
    # encoded through the shared shell variable (the latter hex-encodes the
    # nested command to preserve its quoting).
    assert script.count('"$batch_timeout"') >= 2
    assert "--event-timeout-seconds 7" not in script
    assert "max(0.001" not in script
    assert "d-time.monotonic()" in script


def test_non_pytest_batch_is_killed_and_reaped_at_deadline(
    monkeypatch, tmp_path: Path
) -> None:
    """Execute the emitted shell script, not just its source assertions."""
    monkeypatch.setattr(plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic")
    output = tmp_path / "eval-output"
    output.mkdir()
    plan = {
        "commands": ["python3 -c 'import time; time.sleep(0.4)'", "printf second"],
        "proofs": [{}, {}],
    }
    script = plans.prolite_test_plan_script(
        plan, "f2p", "nonce", controller_timeout=0.08
    ).replace("/eval_output", str(output))

    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert elapsed < 2
    assert (output / "f2p.batch_001.exit").read_text(encoding="utf-8").strip() == "124"
    # The shared deadline is exhausted, so the second batch is recorded as a
    # timeout without starting its command or fabricating a log.
    assert (output / "f2p.batch_002.exit").read_text(encoding="utf-8").strip() == "124"
    assert not (output / "f2p.batch_002.log").exists()


def test_non_pytest_single_batch_receives_controller_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic")
    output = tmp_path / "eval-output"
    output.mkdir()
    script = plans.prolite_test_plan_script(
        {
            "commands": ["python3 -c 'import time; time.sleep(0.3)'"],
            "proofs": [{}],
        },
        "f2p",
        "nonce",
        controller_timeout=0.06,
    ).replace("/eval_output", str(output))

    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )

    assert result.returncode == 124
    assert (output / "f2p.batch_001.exit").read_text(encoding="utf-8").strip() == "124"


def test_shared_deadline_rejects_nonfinite_environment_value(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic"
    )
    plan = {"commands": ["printf should-not-run"], "proofs": [{}]}
    for value in ("nan", "inf", "-inf"):
        output = tmp_path / value.replace("-", "negative-")
        output.mkdir()
        script = plans.prolite_test_plan_script(
            plan,
            "f2p",
            "nonce",
            controller_timeout=1,
            shared_deadline_env="TEST_DEADLINE",
        ).replace("/eval_output", str(output))
        environment = os.environ.copy()
        environment["TEST_DEADLINE"] = value
        result = subprocess.run(
            ["bash", "-s"],
            input=script,
            text=True,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )

        assert result.returncode == 124, (value, result.stdout)
        assert (output / "f2p.batch_001.exit").read_text(encoding="utf-8").strip() == "124"
        assert not (output / "f2p.batch_001.log").exists()


def test_successful_leader_with_descendant_is_cleaned_and_marked_technical(
    monkeypatch, tmp_path: Path
) -> None:
    """A passing command must not leave a same-session background process."""
    monkeypatch.setattr(
        plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic"
    )
    output = tmp_path / "eval-output"
    output.mkdir()
    pid_file = tmp_path / "descendant.pid"
    child_code = "import time; time.sleep(30)"
    command = (
        "import pathlib, subprocess, sys;"
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))"
    )
    script = plans.prolite_test_plan_script(
        {"commands": [f"python3 -c {command!r}"], "proofs": [{}]},
        "f2p",
        "nonce",
        controller_timeout=1,
    ).replace("/eval_output", str(output))

    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )

    assert result.returncode == 125, result.stdout
    assert (output / "f2p.batch_001.exit").read_text(encoding="utf-8").strip() == "125"
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(100):
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        try:
            os.kill(descendant_pid, 9)
        except ProcessLookupError:
            pass
        raise AssertionError("successful bounded command left its descendant running")


def test_cleanup_failure_stops_later_batches(tmp_path: Path, monkeypatch) -> None:
    """Do not launch a later target while an earlier group is unproven."""
    monkeypatch.setattr(
        plans, "validated_test_plan_kind", lambda *args, **kwargs: "synthetic"
    )
    output = tmp_path / "eval-output"
    output.mkdir()
    marker = tmp_path / "second-ran"
    command = (
        "python3 -c 'import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(30)\"]); "
        "time.sleep(0.05)'"
    )
    script = plans.prolite_test_plan_script(
        {"commands": [command, f"touch {marker}"], "proofs": [{}, {}]},
        "f2p",
        "nonce",
        controller_timeout=1,
    ).replace("/eval_output", str(output))
    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )

    assert result.returncode == 125, result.stdout
    assert (output / "f2p.batch_002.exit").read_text(encoding="utf-8").strip() == "125"
    assert not marker.exists()
