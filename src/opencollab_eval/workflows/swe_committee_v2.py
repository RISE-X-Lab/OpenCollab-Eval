"""swe-committee-v2 - committee-style SWE workflow with explicit stage boundaries.

This workflow is implemented from the requested committee graph:
Analyst/Localizer -> Evidence Stage -> Contract Tribunal -> Pre-patch
validation -> Baseline triage -> Coder -> Existing Tests + Approved Validation
-> Patch Attack Stage -> Post-patch validation -> Final skeptic -> Final
verifier, with bounded Coder Minimal Retry rounds.
"""

from __future__ import annotations

from opencollab_eval.workflows import _swe_committee_v2_defs as _definitions
from opencollab_eval.workflows import _swe_committee_v2_impl as _implementation
from opencollab_eval.workflows._swe_committee_v2_impl import swe_committee_v2 as swe_committee_v2

_coder_tools = _implementation._coder_tools
_tester_tools = _implementation._tester_tools

_LEGACY_EXPORTS = tuple(
    sorted({name for module in (_definitions, _implementation) for name in dir(module) if not name.startswith("_")})
)
__all__ = list(_LEGACY_EXPORTS)


def __getattr__(name: str):
    """Preserve direct imports of legacy definition names."""
    if name in _LEGACY_EXPORTS:
        for module in (_definitions, _implementation):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
