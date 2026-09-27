"""Cross-arm alignment: what each experiment arm is actually given, and why.

The comparison this package guards is a paired difference between arms, so
every input that is not the thing under study has to be the same on all of
them. Twelve times an input was not, and each time it was found by listing one
run's inputs by hand and asking of each "is this the thing we are measuring?"
-- never by searching for a keyword, because a drifted default looks exactly
like an aligned one.

``arm_probe`` runs each arm's own entry code far enough to record what reaches
the model. ``arm_registry`` declares what each arm is supposed to get and why.
``arm_audit`` compares the two and names anything that was not declared.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "ARMS",
    "DEFECT",
    "EQUAL",
    "INTENDED",
    "REGISTRY",
    "AlignmentReport",
    "Factor",
    "audit",
    "observe",
]


def __getattr__(name: str) -> Any:
    """Load runtime observers only when their exports are requested."""
    if name in {"AlignmentReport", "audit", "observe"}:
        module = import_module("opencollab_eval.experiment.arm_audit")
    elif name in {"ARMS", "DEFECT", "EQUAL", "INTENDED", "REGISTRY", "Factor"}:
        module = import_module("opencollab_eval.experiment.arm_registry")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value
