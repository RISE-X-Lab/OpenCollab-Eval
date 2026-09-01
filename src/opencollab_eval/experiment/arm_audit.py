"""Work out what every arm is actually given, and compare it to the registry.

``observe`` returns one value per (factor, arm). Every value is either read out
of a call the arm's own code made (see ``arm_probe``) or computed by calling the
function the run itself calls -- ``build_command`` for the argv, ``pool_for``
for the token pool, ``declared_role_tools`` for a team file's bundles. Nothing
is tabulated here; a table would only prove that the table was copied
correctly, and every one of the twelve misalignments found so far looked
correct in the table.

``audit`` compares that against ``arm_registry.REGISTRY`` and reports four
kinds of trouble, of which the first is the reason this exists:

* an *unregistered difference*: the arms differ on a factor in a way the
  registry does not declare. Somebody changed an input and did not say so.
* a *vanished difference*: the registry declares a difference the run no longer
  has. An intended treatment stopped being applied.
* a *stale declaration*: the registry declares a factor or an arm that is no
  longer evaluated.
* a *mismatch*: the declared value and the observed value differ.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencollab.teams import declared_role_names, declared_role_tools

from opencollab_eval.experiment import arm_probe
from opencollab_eval.experiment.arm_registry import (
    ARMS,
    DEFECT,
    EQUAL,
    INTENDED,
    REGISTRY,
    Factor,
    freeze,
)
from opencollab_eval.generation import gen_prediction as _single_generator
from opencollab_eval.generation import gen_prediction_batch as _batch
from opencollab_eval.generation import gen_prediction_constants as _constants
from opencollab_eval.generation import gen_prediction_workflow as _workflow_generator
from opencollab_eval.runtime_config import resolve_runtime_config

#: The team file the ``team`` arm is run with. The batch driver takes it as
#: ``--team-config`` and has no default, so the audit has to name one; this is
#: the file every reported team run has used.
EXPERIMENT_TEAM_CONFIG = "configs/team.handoff.experiment.yaml"

_REPOSITORY_LISTING_HEADERS = ("Repository files", "Repository map")


def default_team_config() -> str:
    """Where the experiment team file is, in either layout this runs in.

    A wheel carries the package without ``configs/``, and CI installs
    OpenCollab from one -- so the checkout it built the wheel from is named by
    ``OPENCOLLAB_SOURCE_ROOT``, which is where the file is then reachable. A
    working tree carries both, so the installed package's own parent answers
    when no source root is named. The path is returned either way: whether it
    exists is the caller's to check and to say something useful about, because
    "the team arm's configuration is not here" is a finding and not a detail.
    """
    import os

    import opencollab

    source_root = os.environ.get("OPENCOLLAB_SOURCE_ROOT", "").strip()
    roots = [Path(source_root)] if source_root else []
    roots.append(Path(opencollab.__file__).resolve().parent.parent)
    for root in roots:
        candidate = root / EXPERIMENT_TEAM_CONFIG
        if candidate.is_file():
            return str(candidate)
    return str(roots[0] / EXPERIMENT_TEAM_CONFIG)


# --------------------------------------------------------------------------
# observation


def _argv_max_steps(arm: str, team_config: str) -> int:
    command = _batch.build_command(
        arm=arm,
        instance_path=Path("instance.json"),
        predictions=Path("preds.jsonl"),
        team_config=Path(team_config),
        budget_per_seat=_constants.DEFAULT_BUDGET,
        max_steps=_constants.DEFAULT_MAX_STEPS,
        timeout=_constants.DEFAULT_TIMEOUT,
        image=None,
    )
    return int(command[command.index("--max-steps") + 1])


def _seat_count(arm: str, team_config: str) -> int:
    if arm in _batch.WORKFLOW_ARMS:
        return _batch.workflow_seats(_batch.WORKFLOW_ARMS[arm])
    if arm in _batch.TEAM_ARMS:
        return len(declared_role_names(team_config))
    return 1


def _image_expression(module: Any, source: str | None = None) -> str:
    """How this generator names the container image, read off its own ``main``.

    Reported as which construction is used rather than as the expression's
    text, so the answer does not move when an unrelated line is reformatted or
    when ``ast.unparse`` changes between Python versions.
    """
    tree = ast.parse(source if source is not None else inspect.getsource(module.main))
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "image" for target in targets
        ):
            continue
        value = node.value
        if value is None:
            continue
        for inner in ast.walk(value):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                if inner.func.id == "default_container_image":
                    return "default_container_image(arch, instance_id)"
        for inner in ast.walk(value):
            if isinstance(inner, ast.JoinedStr):
                return "inline f-string, no shared helper"
        return "unrecognised construction"
    return "no image assignment found"


def _carries_repository_listing(system_prompt: str | None) -> bool:
    """Is a repository listing pasted into this arm's system prefix?

    Reported as present/absent rather than as a byte count: how long the
    listing is depends on the task container, so a count would pin this
    check to the probe's fixture instead of to the arms.
    """
    if not system_prompt:
        return False
    return any(
        header in system_prompt for header in _REPOSITORY_LISTING_HEADERS
    )


def _system_prompt_origin(observation: arm_probe.ArmObservation) -> str:
    if observation.system_prompt is None:
        return "team role cards (the harness supplies no system prompt)"
    stem = observation.system_prompt.split("Repository")[0].strip()
    if stem == _constants.AGENT_PROMPT.strip():
        return "AGENT_PROMPT"
    if stem == _constants.WORKFLOW_AGENT_PROMPT.strip():
        return "WORKFLOW_AGENT_PROMPT"
    return "unrecognised system prompt"


def _tool_bundles(
    arm: str, observation: arm_probe.ArmObservation | None, team_config: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if arm in _batch.TEAM_ARMS:
        return tuple(
            (role, tuple(tools))
            for role, tools in sorted(declared_role_tools(team_config).items())
        )
    assert observation is not None
    return tuple((call.label, call.tools) for call in observation.seat_calls)


def observe(team_config: str | None = None) -> dict[str, dict[str, Any]]:
    """Evaluate every registered factor on every arm, through the real code."""
    import importlib

    _self_collab = importlib.import_module(
        "opencollab_eval.workflows.self_collaboration"
    )

    team_config = team_config or default_team_config()
    workflows = {
        "self-collaboration": _self_collab.self_collaboration,
        "self-collaboration-reading-analyst": (
            _self_collab.self_collaboration_reading_analyst
        ),
    }

    pools = {
        arm: _batch.pool_for(arm, _constants.DEFAULT_BUDGET, Path(team_config))
        for arm in ARMS
    }
    steps = {arm: _argv_max_steps(arm, team_config) for arm in ARMS}
    seats = {arm: _seat_count(arm, team_config) for arm in ARMS}

    observations: dict[str, arm_probe.ArmObservation] = {
        "single": arm_probe.probe_single_arm(
            budget=pools["single"],
            max_steps=steps["single"],
            timeout=_constants.DEFAULT_TIMEOUT,
        ),
        "team": arm_probe.probe_orchestrated_arm(
            "team",
            workflow=None,
            team_config=team_config,
            budget=pools["team"],
            max_steps=steps["team"],
            timeout=_constants.DEFAULT_TIMEOUT,
        ),
    }
    for arm, function in workflows.items():
        observations[arm] = arm_probe.probe_orchestrated_arm(
            arm,
            workflow=function,
            team_config=None,
            budget=pools[arm],
            max_steps=steps[arm],
            timeout=_constants.DEFAULT_TIMEOUT,
        )

    image_expressions = {
        "single": _image_expression(_single_generator),
        **{
            arm: _image_expression(_workflow_generator)
            for arm in ARMS
            if arm != "single"
        },
    }
    temperature = float(resolve_runtime_config(str(Path.cwd()))["temperature"])

    # ``sessions_per_run`` is the number of independent sessions one run opens,
    # because ``--max-steps`` is a ceiling on each of them and not on the run.
    # A workflow arm's count is the number of seats its own script opened on a
    # clean round; a team's is its declared roster, because ``build_scheduler``
    # builds every seat with the same ceiling.
    sessions = {
        "single": len(observations["single"].seat_calls),
        "team": seats["team"],
        **{arm: len(observations[arm].seat_calls) for arm in workflows},
    }

    values: dict[str, dict[str, Any]] = {
        "max_steps_flag": dict(steps),
        "sessions_per_run": dict(sessions),
        "step_ceiling_per_run": {
            arm: steps[arm] * sessions[arm] for arm in ARMS
        },
        "token_pool_per_run": dict(pools),
        "seat_count": dict(seats),
        "token_budget_per_seat": {arm: pools[arm] // seats[arm] for arm in ARMS},
        "role_tool_names": {
            arm: _tool_bundles(arm, observations.get(arm), team_config) for arm in ARMS
        },
        "ask_user_reachable": {
            arm: any(
                "ask_user" in tools
                for _role, tools in _tool_bundles(
                    arm, observations.get(arm), team_config
                )
            )
            for arm in ARMS
        },
        "task_description_sha256": {
            arm: _digest(observations[arm].task_description) for arm in ARMS
        },
        "system_prompt_origin": {
            arm: _system_prompt_origin(observations[arm]) for arm in ARMS
        },
        "system_prompt_carries_repository_listing": {
            arm: _carries_repository_listing(observations[arm].system_prompt)
            for arm in ARMS
        },
        "container_image_expression": image_expressions,
        "sampling_temperature": {arm: temperature for arm in ARMS},
    }
    return {factor: {arm: freeze(value) for arm, value in per_arm.items()}
            for factor, per_arm in values.items()}


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# comparison


@dataclass(frozen=True)
class AlignmentReport:
    """What the run and the registry disagree about."""

    unregistered_factors: tuple[str, ...] = ()
    stale_factors: tuple[str, ...] = ()
    unregistered_arms: tuple[tuple[str, str], ...] = ()
    stale_arms: tuple[tuple[str, str], ...] = ()
    mismatches: tuple[tuple[str, str, Any, Any], ...] = ()
    unregistered_differences: tuple[str, ...] = ()
    vanished_differences: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not (
            self.unregistered_factors
            or self.stale_factors
            or self.unregistered_arms
            or self.stale_arms
            or self.mismatches
            or self.unregistered_differences
            or self.vanished_differences
        )

    def describe(self) -> str:
        if self.ok:
            return "every arm difference is declared in the registry"
        lines: list[str] = []
        for factor in self.unregistered_differences:
            lines.append(
                f"UNREGISTERED DIFFERENCE  {factor}: the arms differ on a factor "
                "the registry declares must be equal"
            )
        for factor in self.vanished_differences:
            lines.append(
                f"VANISHED DIFFERENCE      {factor}: the registry declares a "
                "difference the run no longer has"
            )
        for factor, arm, declared, seen in self.mismatches:
            lines.append(
                f"MISMATCH                 {factor}[{arm}]: "
                f"declared {declared!r}, observed {seen!r}"
            )
        for factor in self.unregistered_factors:
            lines.append(f"UNREGISTERED FACTOR      {factor}")
        for factor in self.stale_factors:
            lines.append(f"STALE DECLARATION        {factor}")
        for factor, arm in self.unregistered_arms:
            lines.append(f"UNREGISTERED ARM         {factor}[{arm}]")
        for factor, arm in self.stale_arms:
            lines.append(f"STALE ARM                {factor}[{arm}]")
        return "\n".join(lines)


def audit(
    observed: Mapping[str, Mapping[str, Any]] | None = None,
    registry: Mapping[str, Factor] = REGISTRY,
) -> AlignmentReport:
    """Compare the observed values against the registry's declarations."""
    observed = observed if observed is not None else observe()
    unregistered_factors = tuple(sorted(set(observed) - set(registry)))
    stale_factors = tuple(sorted(set(registry) - set(observed)))

    unregistered_arms: list[tuple[str, str]] = []
    stale_arms: list[tuple[str, str]] = []
    mismatches: list[tuple[str, str, Any, Any]] = []
    unregistered_differences: list[str] = []
    vanished_differences: list[str] = []

    for name in sorted(set(observed) & set(registry)):
        factor = registry[name]
        seen = observed[name]
        declared = factor.values
        unregistered_arms.extend((name, arm) for arm in sorted(set(seen) - set(declared)))
        stale_arms.extend((name, arm) for arm in sorted(set(declared) - set(seen)))
        for arm in sorted(set(seen) & set(declared)):
            if freeze(seen[arm]) != freeze(declared[arm]):
                mismatches.append((name, arm, declared[arm], seen[arm]))
        differs = len({freeze(value) for value in seen.values()}) > 1
        if factor.verdict == EQUAL and differs:
            unregistered_differences.append(name)
        if factor.verdict in {INTENDED, DEFECT} and not differs:
            vanished_differences.append(name)

    return AlignmentReport(
        unregistered_factors=unregistered_factors,
        stale_factors=stale_factors,
        unregistered_arms=tuple(unregistered_arms),
        stale_arms=tuple(stale_arms),
        mismatches=tuple(mismatches),
        unregistered_differences=tuple(unregistered_differences),
        vanished_differences=tuple(vanished_differences),
    )


def main() -> int:
    report = audit()
    print(report.describe())
    return 0 if report.ok else 1


__all__ = [
    "EXPERIMENT_TEAM_CONFIG",
    "AlignmentReport",
    "audit",
    "default_team_config",
    "main",
    "observe",
]


if __name__ == "__main__":  # pragma: no cover - manual inspection entry point
    raise SystemExit(main())
