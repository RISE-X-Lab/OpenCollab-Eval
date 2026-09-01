"""What the batch driver has to get right before it starts a paid run.

The driver is where an arm stops being a name and becomes an argv and an
environment. Everything checked here is a way that translation goes wrong
without anything crashing: a setting the run was supposed to have that never
reaches it, or an arm that is quietly run as a different arm.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_batch as batch


# --------------------------------------------------------------------------
# the environment a generator is started with


def test_the_driver_pins_the_sampling_temperature_for_every_arm(
    tmp_path: Path,
) -> None:
    # The temperature has to be the same on every arm and it has to be the
    # value the experiment chose, not OpenCollab's framework-wide default --
    # which is a setting every other user of that library shares, and so is not
    # ours to change.
    environment = batch.generator_environment(tmp_path / "logs")

    assert environment["OPENCOLLAB_TEMPERATURE"] == batch.ARM_SAMPLING_TEMPERATURE
    assert environment["OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR"] == str(tmp_path / "logs")


def test_an_ambient_temperature_on_the_machine_does_not_reach_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A batch is run on a shared machine whose shell profile may export
    # anything. The pinned value wins, or the arms are compared at whatever
    # temperature that machine happened to have.
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.9")

    assert (
        batch.generator_environment(tmp_path)["OPENCOLLAB_TEMPERATURE"]
        == batch.ARM_SAMPLING_TEMPERATURE
    )


def test_the_temperature_reaches_the_generator_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end of the chain: what a started subprocess actually has.

    The two tests above check the mapping; this one checks that the mapping is
    the one ``_run_one`` passes to ``subprocess.run``. They were separable
    before -- the driver built an environment and then started the child with
    the ambient one -- and that is exactly the failure that leaves no trace.
    """
    seen: dict[str, str] = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.9")
    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    batch._run_one(command=["true"], log_dir=log_dir)

    assert seen["OPENCOLLAB_TEMPERATURE"] == batch.ARM_SAMPLING_TEMPERATURE
