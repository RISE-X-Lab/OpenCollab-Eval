"""The gate: every difference between arms is one the registry declares.

Twelve times an input to this comparison differed between arms without anyone
having said it should, and each of the twelve landed on an outcome variable --
the delivered patch, the token count, the step count, the resolve verdict.
Every one was found the same way: by listing one run's inputs by hand and
asking of each "is this the thing we are measuring, and who computed this
value?" Keyword search never found any of them, because a drifted default reads
exactly like an aligned one.

This file turns that hand pass into a check that runs unattended. It does not
assert that the arms are identical -- some of them differ on purpose, and those
differences *are* the experiment. It asserts that the set of differences the
code produces is the set the registry declares.

**The controls matter more than the check.** A check that has never been seen
to fail is not evidence that it works, so every factor below is broken on
purpose once -- in the source the audit reads, not in the audit's own output --
and the check is required to notice. The registry itself is mutated the same
way: a declaration is deleted and the check must go red.
"""

from __future__ import annotations

import importlib
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opencollab_eval.experiment import arm_audit, arm_registry
from opencollab_eval.experiment.arm_registry import (
    ARMS,
    DEFECT,
    EQUAL,
    INTENDED,
    REGISTRY,
    Factor,
)
from opencollab_eval.generation import gen_prediction_agent, gen_prediction_batch, gen_prediction_workflow_inputs
from opencollab_eval.generation import gen_prediction_constants as constants

_evaluator = importlib.import_module("opencollab_eval.engine.evaluator")
_self_collab = importlib.import_module("opencollab_eval.workflows.self_collaboration")

#: An evidence anchor has to be somewhere a reader can go and look.
_LOCATION = re.compile(r"^\S+:\d+$")


def _failing_factors(report: arm_audit.AlignmentReport) -> set[str]:
    """Every factor the report has something to say about."""
    named: set[str] = set()
    named.update(report.unregistered_factors)
    named.update(report.stale_factors)
    named.update(report.unregistered_differences)
    named.update(report.vanished_differences)
    named.update(factor for factor, _arm in report.unregistered_arms)
    named.update(factor for factor, _arm in report.stale_arms)
    named.update(factor for factor, _arm, _declared, _seen in report.mismatches)
    return named


# --------------------------------------------------------------------------
# the gate


def test_every_difference_between_the_arms_is_one_the_registry_declares() -> None:
    report = arm_audit.audit()

    assert report.ok, "\n" + report.describe()


def test_the_team_arm_s_configuration_is_reachable_from_here() -> None:
    # Deliberately a failure and not a skip. The team arm is one half of the
    # confirmatory pair, so a run of this suite that cannot see its team file
    # has checked three arms and said nothing about the fourth -- and a check
    # that goes quiet exactly when the thing it guards is missing is the shape
    # this project has been bitten by three times in one day.
    #
    # Reachable means: a source checkout, either the one the installed package
    # came from or the one named by OPENCOLLAB_SOURCE_ROOT. A wheel carries no
    # ``configs/``, so CI has to point that variable at the checkout it built
    # the wheel from, pinned at a commit that has this team file.
    path = Path(arm_audit.default_team_config())

    assert path.is_file(), (
        f"the team arm's configuration is not at {path}. Point "
        "OPENCOLLAB_SOURCE_ROOT at an OpenCollab checkout that carries "
        f"{arm_audit.EXPERIMENT_TEAM_CONFIG}."
    )


def test_the_config_lookup_prefers_the_checkout_the_environment_names() -> None:
    # CI installs OpenCollab from a wheel, which carries no ``configs/``, and
    # names the checkout it was built from. That checkout has to win.
    import opencollab
    from _pytest.monkeypatch import MonkeyPatch

    patch = MonkeyPatch()
    try:
        root = Path(tempfile.mkdtemp())
        planted = root / arm_audit.EXPERIMENT_TEAM_CONFIG
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("entry: solo\nroles:\n  solo:\n    prompt: work\n")
        patch.setenv("OPENCOLLAB_SOURCE_ROOT", str(root))

        assert arm_audit.default_team_config() == str(planted)

        # And the control for the assertion above it: with nothing carrying the
        # file, the lookup reports a path that is not there rather than one
        # that happens to exist.
        patch.setenv("OPENCOLLAB_SOURCE_ROOT", str(root / "empty"))
        patch.setattr(
            opencollab, "__file__", str(root / "site-packages/opencollab/__init__.py")
        )

        assert not Path(arm_audit.default_team_config()).is_file()
    finally:
        patch.undo()


def test_the_registry_covers_exactly_the_arms_the_batch_driver_runs() -> None:
    # A fifth arm added to the driver and not to the registry would otherwise
    # be compared against the other four with nothing checking its inputs.
    assert ARMS == tuple(gen_prediction_batch.ARM_MODULES)


def test_every_factor_the_audit_evaluates_is_declared_and_the_reverse() -> None:
    observed = arm_audit.observe()

    assert set(observed) == set(REGISTRY)
    for factor, per_arm in observed.items():
        assert set(per_arm) == set(ARMS), factor


# --------------------------------------------------------------------------
# the registry has to explain itself


def test_every_declaration_gives_a_reason_and_somewhere_to_check_it() -> None:
    for name, factor in REGISTRY.items():
        assert len(factor.reason.split()) >= 12, name
        assert factor.evidence, name
        assert any(_LOCATION.match(anchor) for anchor in factor.evidence), name


def test_every_anchor_into_this_repository_points_at_a_file_that_is_there() -> None:
    # Line numbers drift and that is tolerable -- each reason also names the
    # identifier it is about. A path that no longer exists is not tolerable:
    # it means the code the reason describes was moved or deleted, and nobody
    # revisited the declaration that rests on it.
    #
    # Only anchors into this repository are checked. The OpenCollab ones point
    # into a checkout whose commit CI pins separately, so a miss there would
    # say something about the pin rather than about this registry.
    root = Path(
        os.environ.get("OPENCOLLAB_EVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1])
    )
    prefix = "OpenCollab-Eval/"
    for name, factor in REGISTRY.items():
        for anchor in factor.evidence:
            path_text, _, line_text = anchor.rpartition(":")
            if not path_text.startswith(prefix):
                continue
            path = root / path_text[len(prefix):]
            assert path.is_file(), f"{name}: {anchor}"
            lines = path.read_text(encoding="utf-8").splitlines()
            assert 0 < int(line_text) <= len(lines), f"{name}: {anchor}"


def test_a_declaration_with_no_reason_is_refused() -> None:
    # The rule this pins: an entry may not be added to the registry by writing
    # down a value and leaving the "why" blank. A blank reason turns the table
    # back into the thing it replaced.
    with pytest.raises(ValueError, match="needs a reason"):
        Factor(
            name="unexplained",
            verdict=EQUAL,
            values={arm: 1 for arm in ARMS},
            reason="   ",
            evidence=("somewhere.py:1",),
        )


def test_a_reason_with_nowhere_to_check_it_is_refused() -> None:
    with pytest.raises(ValueError, match="somewhere to check it"):
        Factor(
            name="unanchored",
            verdict=EQUAL,
            values={arm: 1 for arm in ARMS},
            reason="because it has always been that way, and everyone knows it",
            evidence=(),
        )


def test_a_defect_must_name_the_outcome_variable_it_lands_on() -> None:
    # A difference nobody intends is only worth carrying in the registry if it
    # reaches something measured; if it does, the paper has to say so.
    with pytest.raises(ValueError, match="outcome variable"):
        Factor(
            name="unattributed",
            verdict=DEFECT,
            values={arm: index for index, arm in enumerate(ARMS)},
            reason="a difference that nobody has got round to explaining yet",
            evidence=("somewhere.py:1",),
        )


def test_each_declared_defect_names_an_outcome_and_each_other_factor_does_not() -> None:
    for name, factor in REGISTRY.items():
        if factor.verdict == DEFECT:
            assert (factor.outcome or "").strip(), name
        else:
            assert factor.outcome is None, name


def test_the_runtime_only_list_says_where_each_quantity_is_read() -> None:
    # The audit must not compute these from the source: they are properties of
    # a run. Saying so is only useful if it also says where to look.
    for quantity, where in arm_registry.RUNTIME_ONLY:
        assert quantity.strip()
        assert len(where.split()) >= 5, quantity


# --------------------------------------------------------------------------
# mutation: remove a declaration, the check must go red


def test_deleting_a_factor_from_the_registry_turns_the_check_red() -> None:
    observed = arm_audit.observe()
    for name in REGISTRY:
        thinner = {key: value for key, value in REGISTRY.items() if key != name}

        report = arm_audit.audit(observed, thinner)

        assert not report.ok, name
        assert name in report.unregistered_factors


def test_deleting_one_arm_from_a_declaration_turns_the_check_red() -> None:
    observed = arm_audit.observe()
    for name, factor in REGISTRY.items():
        for arm in ARMS:
            trimmed = dict(REGISTRY)
            trimmed[name] = replace(
                factor,
                values={
                    other: value
                    for other, value in factor.values.items()
                    if other != arm
                },
            )

            report = arm_audit.audit(observed, trimmed)

            assert not report.ok, (name, arm)
            assert (name, arm) in report.unregistered_arms


def test_changing_a_declared_value_turns_the_check_red() -> None:
    observed = arm_audit.observe()
    for name, factor in REGISTRY.items():
        altered = dict(REGISTRY)
        values = dict(factor.values)
        values["single"] = ("a value nobody declared",)
        altered[name] = replace(factor, values=values)

        report = arm_audit.audit(observed, altered)

        assert not report.ok, name
        assert name in {entry[0] for entry in report.mismatches}


def test_calling_an_intended_difference_equal_turns_the_check_red() -> None:
    # The registry's verdicts are load-bearing, not documentation: relabelling
    # a treatment as "these must match" has to fail rather than pass quietly.
    observed = arm_audit.observe()
    for name, factor in REGISTRY.items():
        if factor.verdict == EQUAL:
            continue
        relabelled = dict(REGISTRY)
        relabelled[name] = replace(factor, verdict=EQUAL, outcome=None)

        report = arm_audit.audit(observed, relabelled)

        assert name in report.unregistered_differences, name


def test_calling_an_equal_factor_a_treatment_turns_the_check_red() -> None:
    observed = arm_audit.observe()
    for name, factor in REGISTRY.items():
        if factor.verdict != EQUAL:
            continue
        relabelled = dict(REGISTRY)
        relabelled[name] = replace(factor, verdict=INTENDED)

        report = arm_audit.audit(observed, relabelled)

        assert name in report.vanished_differences, name


# --------------------------------------------------------------------------
# positive controls: break the source, the check must notice
#
# Each case perturbs something the audit *reads* -- a constant, a team file, a
# function the run calls -- and never the audit's own output, so what is being
# demonstrated is that the evaluation reaches the real thing.


def _four_role_team(tmp_path: Path) -> str:
    path = tmp_path / "team.four-roles.yaml"
    path.write_text(
        "entry: analyst\n"
        "roles:\n"
        + "".join(
            f"  {role}:\n    prompt: you are the {role}\n"
            f"    tools: [bash, file_read, grep, message_agent, run_tests, submit]\n"
            for role in ("analyst", "coder", "tester", "reviewer")
        )
        + "topology:\n"
        + "".join(
            f"  {role}: [{', '.join(o for o in ('analyst', 'coder', 'tester', 'reviewer') if o != role)}]\n"
            for role in ("analyst", "coder", "tester", "reviewer")
        ),
        encoding="utf-8",
    )
    return str(path)


def _team_with_ask_user(tmp_path: Path) -> str:
    """The experiment team's roster with ``ask_user`` back on the Analyst.

    Written from scratch rather than copied: the real file names its role cards
    by relative path, and a copy somewhere else would fail to load for a reason
    that has nothing to do with the control.
    """
    path = tmp_path / "team.ask-user.yaml"
    bundles = {
        "analyst": "[apply_patch, ask_user, bash, file_read, file_write, grep,"
        " message_agent, run_tests, submit, team_status]",
        "coder": "[apply_patch, bash, file_read, file_write, grep, message_agent,"
        " run_tests, submit, team_status]",
        "tester": "[bash, file_read, git_diff, grep, message_agent, run_tests,"
        " submit, team_status]",
    }
    roles = "".join(
        f"  {role}:\n    prompt: you are the {role}\n    tools: {tools}\n"
        for role, tools in bundles.items()
    )
    topology = "".join(
        f"  {role}: [{', '.join(other for other in bundles if other != role)}]\n"
        for role in bundles
    )
    path.write_text(
        f"entry: analyst\nroles:\n{roles}topology:\n{topology}", encoding="utf-8"
    )
    return str(path)


def _break_max_steps(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setattr(constants, "DEFAULT_MAX_STEPS", 40)
    return "max_steps_flag", {}


def _break_sessions_per_run(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    return "sessions_per_run", {"team_config": _four_role_team(tmp_path)}


def _break_step_ceiling(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    # The other half of the product: the flag is untouched and the number of
    # sessions it applies to moves, which is the shape the defect itself has.
    return "step_ceiling_per_run", {"team_config": _four_role_team(tmp_path)}


def _break_token_pool(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setattr(_self_collab, "SEATS", 4)
    return "token_pool_per_run", {}


def _break_seat_count(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setattr(_self_collab, "SEATS", 4)
    return "seat_count", {}


def _break_budget_per_seat(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    # The allocation this project actually shipped once: hand a team the pool a
    # solo agent gets, and every seat silently holds a third of it.
    monkeypatch.setattr(
        gen_prediction_batch,
        "pool_for",
        lambda arm, budget_per_seat, team_config: budget_per_seat,
    )
    return "token_budget_per_seat", {}


def _break_role_tools(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setattr(
        gen_prediction_agent,
        "WORKING_TOOL_NAMES",
        tuple(name for name in constants.WORKING_TOOL_NAMES if name != "apply_patch"),
    )
    return "role_tool_names", {}


def _break_ask_user(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    return "ask_user_reachable", {"team_config": _team_with_ask_user(tmp_path)}


def _break_task_text(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    # The historical shape of this one: one arm's builder gained a paragraph
    # the other's did not have.
    monkeypatch.setattr(
        gen_prediction_workflow_inputs,
        "BLIND_VALIDATION_BLOCK",
        "## Grading\nThe harness will run its own tests.\n",
    )
    return "task_description_sha256", {}


def _break_system_prompt_origin(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setattr(
        gen_prediction_agent,
        "AGENT_PROMPT",
        "You are an autonomous engineer. Apply the fix as soon as you know it.\n",
    )
    return "system_prompt_origin", {}


def _break_repository_listing(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    async def no_listing(_env):
        return ""

    monkeypatch.setattr(_evaluator, "build_repository_map", no_listing)
    return "system_prompt_carries_repository_listing", {}


def _break_temperature(monkeypatch, tmp_path) -> tuple[str, dict[str, Any]]:
    monkeypatch.setenv("OPENCOLLAB_TEMPERATURE", "0.9")
    return "sampling_temperature", {}


_CONTROLS = (
    _break_max_steps,
    _break_sessions_per_run,
    _break_step_ceiling,
    _break_token_pool,
    _break_seat_count,
    _break_budget_per_seat,
    _break_role_tools,
    _break_ask_user,
    _break_task_text,
    _break_system_prompt_origin,
    _break_repository_listing,
    _break_temperature,
)


@pytest.mark.parametrize("control", _CONTROLS, ids=lambda fn: fn.__name__[7:])
def test_breaking_a_factor_in_the_source_turns_the_check_red(
    control, monkeypatch, tmp_path
) -> None:
    factor, kwargs = control(monkeypatch, tmp_path)

    report = arm_audit.audit(arm_audit.observe(**kwargs))

    assert not report.ok, f"{factor}: the audit did not notice"
    assert factor in _failing_factors(report), (
        f"{factor}: the audit went red but blamed "
        f"{sorted(_failing_factors(report))}\n{report.describe()}"
    )


def test_the_container_image_construction_control_is_read_off_the_source() -> None:
    # This factor is evaluated by reading each generator's own ``main``, so the
    # control feeds it an edited copy of that source rather than patching a
    # value. Both directions are exercised: the shared helper and the f-string.
    helper = "def main():\n    image = args.image or default_container_image(a, i)\n"
    inline = "def main():\n    image = args.image or f'sweb.eval.{a}.{i}:latest'\n"

    assert (
        arm_audit._image_expression(None, source=helper)
        == "default_container_image(arch, instance_id)"
    )
    assert (
        arm_audit._image_expression(None, source=inline)
        == "inline f-string, no shared helper"
    )
    assert arm_audit._image_expression(None, source="def main():\n    pass\n") == (
        "no image assignment found"
    )


def test_breaking_the_container_image_construction_turns_the_check_red(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        arm_audit,
        "_image_expression",
        lambda module, source=None: "a construction nobody declared",
    )

    report = arm_audit.audit(arm_audit.observe())

    assert not report.ok
    assert "container_image_expression" in _failing_factors(report)
