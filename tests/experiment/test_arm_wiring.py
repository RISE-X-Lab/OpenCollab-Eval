"""What the batch driver has to get right before it starts a paid run.

The driver is where an arm stops being a name and becomes an argv and an
environment. Everything checked here is a way that translation goes wrong
without anything crashing: a setting the run was supposed to have that never
reaches it, or an arm that is quietly run as a different arm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opencollab_eval.generation import gen_prediction_batch as batch

# --------------------------------------------------------------------------
# an arm that is not wired up has to fail loudly


def test_an_orchestrated_arm_that_names_no_solver_is_refused() -> None:
    """The failure this guards is silent from end to end.

    ``gen_prediction_workflow`` resolves its solver as: a team file, else a
    named workflow, else the built-in ``generate_review_fix``. An arm added to
    ARM_MODULES on that generator and to neither solver table therefore runs
    the review-fix workflow, exits zero, and appends its output to
    ``preds-<arm>.jsonl`` under the new arm's name. The arm that was meant to
    be measured is never run, and nothing in the batch's output differs from a
    batch that worked.
    """
    with pytest.raises(ValueError, match="names no solver"):
        batch.validate_arm_wiring(
            arm_modules={**batch.ARM_MODULES, "reviewer": batch.ORCHESTRATED_GENERATOR},
            team_arms=batch.TEAM_ARMS,
            workflow_arms=batch.WORKFLOW_ARMS,
        )


def test_an_arm_that_is_both_a_team_and_a_workflow_is_refused() -> None:
    with pytest.raises(ValueError, match="both team and workflow"):
        batch.validate_arm_wiring(
            arm_modules=batch.ARM_MODULES,
            team_arms=frozenset({"team", "self-collaboration"}),
            workflow_arms=batch.WORKFLOW_ARMS,
        )


def test_solver_configuration_for_an_arm_that_does_not_exist_is_refused() -> None:
    # The half-finished rename: the arm was renamed in ARM_MODULES and the
    # workflow table still configures the old name, so the arm that does run is
    # configured by nothing.
    with pytest.raises(ValueError, match="cannot run"):
        batch.validate_arm_wiring(
            arm_modules=batch.ARM_MODULES,
            team_arms=batch.TEAM_ARMS,
            workflow_arms={**batch.WORKFLOW_ARMS, "self-collab": "self-collaboration"},
        )


def test_a_solver_flag_on_an_arm_that_cannot_read_one_is_refused() -> None:
    with pytest.raises(ValueError, match="only generator that reads one"):
        batch.validate_arm_wiring(
            arm_modules=batch.ARM_MODULES,
            team_arms=frozenset({"team", "single"}),
            workflow_arms=batch.WORKFLOW_ARMS,
        )


def test_the_built_command_is_refused_when_it_names_no_solver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same defect caught one layer later, at the argv itself, which is
    # where a table monkeypatched at run time shows up.
    monkeypatch.setitem(batch.ARM_MODULES, "reviewer", batch.ORCHESTRATED_GENERATOR)

    with pytest.raises(ValueError, match="does not name the solver"):
        batch.build_command(
            arm="reviewer",
            instance_path=tmp_path / "a-1.json",
            predictions=tmp_path / "preds.jsonl",
            team_config=None,
            budget_per_seat=1000,
            max_steps=5,
            timeout=60.0,
            image=None,
        )


def test_an_unknown_arm_is_refused_rather_than_funded_for_one_seat() -> None:
    # A single seat's budget is a plausible answer, and the wrong one: it is
    # what an unregistered arm used to be given.
    with pytest.raises(ValueError, match="unknown arm"):
        batch.pool_for("no-such-arm", 2_000_000, None)


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

    assert batch.generator_environment(tmp_path)["OPENCOLLAB_TEMPERATURE"] == batch.ARM_SAMPLING_TEMPERATURE


def test_the_temperature_reaches_the_generator_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real child receives the selected temperature through the owned process runner."""
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.9")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    returncode, _elapsed = batch._run_one(
        command=[sys.executable, "-c", "import os; print(os.environ['OPENCOLLAB_TEMPERATURE'])"],
        log_dir=log_dir,
        stop=batch.BatchStop(),
    )
    assert returncode == 0
    assert (log_dir / "driver.log").read_text().strip() == batch.ARM_SAMPLING_TEMPERATURE
