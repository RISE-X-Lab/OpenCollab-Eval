"""validation-council-solve - contract-led validation council for SWE tasks.

This workflow turns a SWE-style issue into a compact evidence package with
localization, behavior contracts, repository test cartography, approved public
probes, baseline triage, and one authoritative coding role. The first nonempty
source candidate is frozen for external official evaluation.

It is designed for blind SWE-bench use. Roles may inspect only the issue text,
repository code, public tests, and public documentation. They must not rely on
official hidden tests, injected grader patches, or FAIL_TO_PASS node ids.
"""

from __future__ import annotations

from opencollab_eval.workflows import _validation_council_solve_defs as _definitions
from opencollab_eval.workflows import _validation_council_solve_impl as _implementation
from opencollab_eval.workflows._validation_council_solve_impl import (
    validation_council_solve as validation_council_solve,
)

_coder_tools = _definitions._coder_tools
_tester_tools = _definitions._tester_tools

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
