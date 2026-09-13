"""Evaluation solver workflows."""

from .analyst_solve import analyst_solve, team_pro
from .base_team import base_team
from .base_team_single_pass import base_team_single_pass_v1
from .scout_solve import scout_solve
from .self_collab import self_collab
from .split_solve import split_solve
from .swe_committee_v2 import swe_committee_v2
from .validation_council_dual_coder_contract import (
    validation_council_dual_coder_contract_v1,
)
from .validation_council_solve import validation_council_solve
from .validation_council_triple_coder_contract import (
    validation_council_triple_coder_contract_v1,
)

__all__ = [
    "analyst_solve",
    "base_team",
    "base_team_single_pass_v1",
    "scout_solve",
    "self_collab",
    "split_solve",
    "swe_committee_v2",
    "team_pro",
    "validation_council_dual_coder_contract_v1",
    "validation_council_triple_coder_contract_v1",
    "validation_council_solve",
]
