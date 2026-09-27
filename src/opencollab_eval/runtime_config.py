"""Resolve OpenCollab runtime settings through its public configuration view."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opencollab.profiles import resolve_profile_name

SINGLE2_AUTHORIZED_BUDGET = 1_000_000_000_000
SINGLE2_AUTHORIZED_MAX_STEPS = 1_000_000_000_000


def resolve_runtime_config(
    workspace: str | Path,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-secret effective settings for evaluation evidence and calls."""
    from opencollab import OpenCollab

    values = dict(overrides or {})
    client = OpenCollab(
        workspace,
        model=values.pop("model", None),
        provider=values.pop("provider", None),
        base_url=values.pop("base_url", None),
        config=values,
    )
    return dict(client.configuration)


__all__ = ["resolve_runtime_config"]


def resolve_workflow_agent_profile(profile: str | None) -> str | None:
    """Keep omitted role configuration and normalize explicit profile names."""
    return None if profile is None else resolve_profile_name(profile)


def resolve_solver_agent_profile(workflow, profile: str | None) -> str | None:
    """Resolve standalone Base defaults separately from workflow role defaults."""
    if workflow == "single2":
        return resolve_profile_name("single2")
    if workflow is None or workflow == "single-agent":
        return resolve_profile_name(profile)
    return resolve_workflow_agent_profile(profile)


def resolve_generation_environment(configured, *, environment=None):
    """Inherit the host limit toggle and let explicit workflow settings win."""
    inherited = os.environ if environment is None else environment
    effective = {}
    if "OPENCOLLAB_UNBOUNDED_LIMITS" in inherited:
        effective["OPENCOLLAB_UNBOUNDED_LIMITS"] = str(inherited["OPENCOLLAB_UNBOUNDED_LIMITS"])
    effective.update({str(key): str(value) for key, value in configured.items()})
    return effective


def effective_generation_limits(*, budget, max_steps, environment=None):
    """Resolve the same effective limits for generation and persisted identity."""
    values = resolve_generation_environment({} if environment is None else environment)
    unbounded = str(values.get("OPENCOLLAB_UNBOUNDED_LIMITS", "")).strip().lower()
    if unbounded not in {"", "0", "false", "1", "true"}:
        raise ValueError("OPENCOLLAB_UNBOUNDED_LIMITS must be true or false")
    return (None, None) if unbounded in {"1", "true"} else (budget, max_steps)


def resolve_agent_generation_limits(profile, max_steps, budget):
    """Restore Single2's explicit evaluation limits after unbounded parsing."""
    profile = resolve_profile_name(profile)
    if profile == "single2":
        return (
            SINGLE2_AUTHORIZED_MAX_STEPS if max_steps is None else max_steps,
            SINGLE2_AUTHORIZED_BUDGET if budget is None else budget,
        )
    return max_steps, budget


def runtime_identity_limits(workflow, budget, max_steps, environment=None, *, agent_profile=None):
    """Return the limits that the selected generator records in its metric."""
    effective_budget, effective_max_steps = effective_generation_limits(
        budget=budget,
        max_steps=max_steps,
        environment=environment,
    )
    if workflow in {"single-agent", "single2"}:
        effective_max_steps, effective_budget = resolve_agent_generation_limits(
            resolve_solver_agent_profile(workflow, agent_profile),
            effective_max_steps,
            effective_budget,
        )
    return effective_budget, effective_max_steps
