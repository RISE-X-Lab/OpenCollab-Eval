"""Evaluation solver workflows."""

from .analyst_solve import analyst_solve, team_pro
from .base_team import base_team
from .scout_solve import scout_solve
from .self_collab import self_collab
from .split_solve import split_solve
from .swe_committee_v2 import swe_committee_v2
from .validation_council_solve import validation_council_solve

__all__ = [
    "analyst_solve",
    "base_team",
    "scout_solve",
    "self_collab",
    "split_solve",
    "swe_committee_v2",
    "team_pro",
    "validation_council_solve",
]
