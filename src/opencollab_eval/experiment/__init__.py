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

from opencollab_eval.experiment.arm_audit import (
    AlignmentReport,
    audit,
    observe,
)
from opencollab_eval.experiment.arm_registry import (
    ARMS,
    DEFECT,
    EQUAL,
    INTENDED,
    REGISTRY,
    Factor,
)

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
